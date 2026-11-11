import logging

import pytest

from app.logship import LogShipper


class FakeClient:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent = []   # (stream, lines, batch_id)

    def post_logs(self, stream, lines, batch_id):
        self.sent.append((stream, lines, batch_id))
        return self.results.pop(0) if self.results else "ok"


@pytest.fixture
def make():
    shippers = []

    def build(client=None, **kwargs):
        shipper = LogShipper(client or FakeClient(), flush_interval_s=3600, **kwargs)
        shippers.append(shipper)
        return shipper
    yield build
    for shipper in shippers:
        shipper.close()


def record(name, level, message, **kwargs):
    return logging.LogRecord(name, level, __file__, 1, message, (), None, **kwargs)


def test_lines_are_grouped_into_the_tick_and_backup_logs(make):
    client = FakeClient()
    shipper = make(client)
    shipper.emit(record("app.tick", logging.INFO, "tick complete"))
    shipper.emit(record("app.runner", logging.ERROR, "jim-home - Backup failed"))
    shipper.emit(record("app.restic", logging.WARNING, "slow"))
    shipper.flush()
    by_stream = {stream: lines for stream, lines, _ in client.sent}
    assert [l["message"] for l in by_stream["tick"]] == ["tick complete"]
    assert [(l["level"], l["message"]) for l in by_stream["backup"]] == [
        ("ERROR", "jim-home - Backup failed"), ("WARNING", "slow")]
    assert by_stream["tick"][0]["ts"] > 0


def test_formatted_arguments_and_tracebacks_are_included(make):
    client = FakeClient()
    shipper = make(client)
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        rec = logging.LogRecord("app.runner", logging.ERROR, __file__, 1, "crashed %s", ("job",), sys.exc_info())
    shipper.emit(rec)
    shipper.flush()
    message = client.sent[0][1][0]["message"]
    assert message.startswith("crashed job") and "ValueError: kaboom" in message


def test_the_dashboard_clients_own_logging_is_not_shipped(make):
    client = FakeClient()
    shipper = make(client)
    shipper.emit(record("app.dashboard", logging.WARNING, "dashboard rejected"))
    shipper.emit(record("app.logship", logging.WARNING, "x"))
    shipper.flush()
    assert client.sent == []


def test_a_batch_is_retried_with_the_same_id_until_accepted(make):
    client = FakeClient(results=["retry", "retry", "ok"])
    shipper = make(client)
    shipper.emit(record("app.tick", logging.INFO, "one"))
    shipper.flush()
    shipper.emit(record("app.tick", logging.INFO, "two"))  # arrives while the first batch is still unsent
    shipper.flush()
    shipper.flush()  # the third attempt is accepted and "two" follows in a batch of its own
    first = [call for call in client.sent if call[1][0]["message"] == "one"]
    second = [call for call in client.sent if call[1][0]["message"] == "two"]
    assert len(first) == 3 and len({batch_id for _, _, batch_id in first}) == 1
    assert len(second) == 1 and second[0][2] != first[0][2]


def test_a_refused_batch_is_dropped_not_retried(make):
    client = FakeClient(results=["drop"])
    shipper = make(client)
    shipper.emit(record("app.tick", logging.INFO, "bad"))
    shipper.flush()
    shipper.flush()
    assert len(client.sent) == 1 and shipper.pending_count() == 0


def test_the_buffer_is_capped_and_the_oldest_lines_go_first(make):
    client = FakeClient(results=["retry"] * 50)
    shipper = make(client, max_buffer=3, batch_size=10)
    for i in range(10):
        shipper.emit(record("app.tick", logging.INFO, f"line {i}"))
    assert shipper.pending_count("tick") == 3
    client.results = []
    shipper.flush()
    assert [l["message"] for l in client.sent[-1][1]] == ["line 7", "line 8", "line 9"]


def test_large_backlogs_are_split_into_batches(make):
    client = FakeClient()
    shipper = make(client, batch_size=4)
    for i in range(10):
        shipper.emit(record("app.tick", logging.INFO, f"l{i}"))
    shipper.flush()
    assert [len(lines) for _, lines, _ in client.sent] == [4, 4, 2]
    assert len({batch_id for _, _, batch_id in client.sent}) == 3


def test_long_lines_are_cut(make):
    client = FakeClient()
    shipper = make(client)
    shipper.emit(record("app.tick", logging.INFO, "x" * 5000))
    shipper.flush()
    assert len(client.sent[0][1][0]["message"]) == 2000


def test_close_sends_what_is_left(make):
    client = FakeClient()
    shipper = LogShipper(client, flush_interval_s=3600)
    shipper.emit(record("app.tick", logging.INFO, "last words"))
    shipper.close()
    assert client.sent[0][1][0]["message"] == "last words"


def test_the_background_thread_flushes_on_its_own():
    client = FakeClient()
    shipper = LogShipper(client, flush_interval_s=0.05)
    try:
        shipper.emit(record("app.tick", logging.INFO, "hello"))
        for _ in range(100):
            if client.sent:
                break
            import time
            time.sleep(0.02)
        assert client.sent and client.sent[0][1][0]["message"] == "hello"
    finally:
        shipper.close()


def test_a_broken_record_never_raises(make):
    shipper = make()

    class Bad:
        def __str__(self):
            raise RuntimeError("no string for you")
    shipper.emit(record("app.tick", logging.INFO, Bad()))
    shipper.flush()
