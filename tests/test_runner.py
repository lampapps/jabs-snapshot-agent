import pytest

from app import locking, runner, state
from app.config import Config, Job, ResticOptions
from app.restic import ResticError

from .conftest import write_config


class Recorder:
    """Stands in for the dashboard client and records every call."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True
        return record

    def names(self):
        return [name for name, _, _ in self.calls]

    def first(self, name):
        return next(c for c in self.calls if c[0] == name)


def make_job(**overrides) -> Job:
    values = dict(name="jim-home", path="x.yaml", repo_path="/repo", paths=["/data"], tags=["jim-home"], schedules=[],
                  prune_after_backup=False, verify_after_backup=True, retention={"keep_daily": 7},
                  restic_options=ResticOptions(), exclude=["*.tmp"])
    values.update(overrides)
    return Job(**values)


SUMMARY = {"message_type": "summary", "snapshot_id": "a" * 64, "files_new": 2, "files_changed": 1, "data_added": 500}


@pytest.fixture
def env(home, monkeypatch):
    """A restic that works, with every call recorded; individual tests break parts of it."""
    monkeypatch.setenv("RESTIC_PASSWORD", "pw")
    calls = []
    notices = []
    pings = []

    def record(name, result=None):
        def fake(*args, **kwargs):
            calls.append((name, args, kwargs))
            if isinstance(result, Exception):
                raise result
            return result
        return fake

    fakes = {"repo_exists": True, "backup": SUMMARY, "check": None, "forget_prune": [], "init_repo": None}
    for name, result in fakes.items():
        monkeypatch.setattr(runner.restic, name, record(name, result))
    monkeypatch.setattr(runner.mailer, "notify", lambda email, kind, subject, body: notices.append((kind, subject, body)))
    monkeypatch.setattr(runner.kuma, "ping", lambda status, message="", dry_run=False: pings.append((status, message, dry_run)))
    monkeypatch.setattr(runner.storage, "source_size_sample",
                        lambda cfg, job: {"name": job.name, "series": "source", "kind": "repo", "used_bytes": 9})
    monkeypatch.setattr(runner.storage, "repo_size_sample",
                        lambda cfg, path: {"name": "Restic repo", "kind": "repo", "used_bytes": 99})

    class Env:
        pass
    e = Env()
    e.calls, e.notices, e.pings, e.monkeypatch = calls, notices, pings, monkeypatch
    e.restic_calls = lambda name: [c for c in calls if c[0] == name]

    def fail(name, message="boom"):
        monkeypatch.setattr(runner.restic, name, record(name, ResticError(message)))
    e.fail = fail
    return e


def run(job=None, client=None, cfg=None, **kwargs):
    return runner.run_job(job or make_job(), cfg or Config(), client or Recorder(), **kwargs)


class TestSuccess:
    def test_the_dashboard_sees_the_whole_lifecycle(self, env):
        client = Recorder()
        assert run(client=client) is True
        assert client.names() == ["send_backup_start", "send_backup_stage", "send_backup_complete",
                                  "send_backup_stage", "send_check_result", "post_storage"]
        complete = client.first("send_backup_complete")
        assert complete[1][:4] == ("jim-home", "snapshot", "jim-home", "jim-home")  # target_id is the job name
        assert complete[2]["success"] is True and complete[2]["snapshot_id"] == "a" * 64
        assert (complete[2]["files_backed_up"], complete[2]["bytes_backed_up"]) == (3, 500)
        run_ids = {c[2]["run_id"] for c in client.calls if "run_id" in c[2]}
        assert len(run_ids) == 1

    def test_backup_receives_the_job_settings(self, env):
        run(job=make_job(tags=["t1", "t2"], paths=["/a", "/b"], exclude=["x"]))
        _, args, kwargs = env.restic_calls("backup")[0]
        assert args[:2] == ("/repo", ["/a", "/b"])
        assert kwargs["tags"] == ["t1", "t2"] and kwargs["exclude_patterns"] == ["x"] and kwargs["password"] == "pw"

    def test_the_result_is_remembered_and_kuma_is_pinged(self, env):
        run()
        last = state.read()["jobs"]["jim-home"]["last_run"]
        assert last["success"] is True and last["snapshot_id"] == "a" * 64 and last["files"] == 3
        assert env.pings == [("up", "jim-home: OK", False)]

    def test_the_lock_is_released(self, env, home):
        run()
        assert not list((home / "data" / "locks").glob("*.lock"))

    def test_email_on_completion_goes_through_the_notify_filter(self, env):
        run()
        assert [n[0] for n in env.notices] == ["backup_complete"]

    def test_standalone_sends_nothing_to_a_dashboard_and_measures_nothing(self, env):
        client = Recorder(enabled=False)
        run(client=client)
        assert "post_storage" not in client.names()
        assert state.read()["jobs"]["jim-home"]["last_run"]["success"] is True


class TestStorageReport:
    def test_source_size_is_reported_after_a_backup(self, env):
        client = Recorder()
        run(client=client)
        (samples,) = client.first("post_storage")[1]
        assert samples == [{"name": "jim-home", "series": "source", "kind": "repo", "used_bytes": 9}]

    def test_repo_size_is_added_after_a_prune(self, env):
        client = Recorder()
        run(job=make_job(prune_after_backup=True), client=client)
        (samples,) = client.first("post_storage")[1]
        assert [s["name"] for s in samples] == ["jim-home", "Restic repo"]

    def test_both_can_be_switched_off(self, env):
        client = Recorder()
        run(client=client, cfg=Config(storage={"source_size": False}))
        assert client.first("post_storage")[1] == ([],)
        client = Recorder()
        run(client=client, cfg=Config(storage={"report": False}))
        assert "post_storage" not in client.names()

    def test_dry_runs_report_nothing(self, env):
        client = Recorder()
        run(client=client, dry_run=True)
        assert "post_storage" not in client.names()
        assert env.pings == [("up", "[DRY RUN] jim-home: OK", True)]  # kuma.ping itself ignores dry runs


class TestVerifyAndPrune:
    def test_check_can_be_disabled(self, env):
        run(job=make_job(verify_after_backup=False))
        assert env.restic_calls("check") == []

    def test_a_failing_check_alerts_but_the_backup_stays_successful(self, env):
        env.fail("check", "bad pack")
        client = Recorder()
        assert run(client=client) is True
        assert client.first("send_check_result")[2]["success"] is False
        assert ("error", "JABS snapshot repository check FAILED: jim-home") == env.notices[-1][:2]

    def test_prune_uses_the_retention_and_reports_removed_snapshots(self, env):
        env.monkeypatch.setattr(runner.restic, "forget_prune", lambda *a, **k: env.calls.append(("forget_prune", a, k)) or ["x" * 64])
        client = Recorder()
        run(job=make_job(prune_after_backup=True), client=client)
        _, args, kwargs = env.restic_calls("forget_prune")[0]
        assert kwargs["keep_daily"] == 7 and kwargs["tags"] == ["jim-home"] and args == ("/repo",)
        assert client.first("send_target_purged")[1] == ("jim-home", ["x" * 64])

    def test_pruning_can_be_requested_on_the_command_line(self, env):
        run(do_prune=True)
        assert len(env.restic_calls("forget_prune")) == 1

    def test_a_failing_prune_is_only_logged(self, env):
        env.fail("forget_prune")
        client = Recorder()
        assert run(job=make_job(prune_after_backup=True), client=client) is True
        assert "send_target_purged" not in client.names()


class TestFailures:
    def test_a_restic_error_is_reported_everywhere(self, env):
        env.fail("backup", "disk full")
        client = Recorder()
        assert run(client=client) is False
        complete = client.first("send_backup_complete")
        assert complete[2]["success"] is False and "disk full" in complete[2]["error_message"]
        assert env.notices[0][:2] == ("error", "JABS snapshot backup FAILED: jim-home")
        last = state.read()["jobs"]["jim-home"]["last_run"]
        assert last["success"] is False and "disk full" in last["error"]
        assert env.pings[0][0] == "down"

    def test_a_missing_repository_needs_init(self, env):
        env.monkeypatch.setattr(runner.restic, "repo_exists", lambda *a, **k: False)
        client = Recorder()
        assert run(client=client) is False
        assert "does not exist" in client.first("send_backup_complete")[2]["error_message"]
        assert env.restic_calls("backup") == []

    def test_init_creates_the_repository_first(self, env):
        env.monkeypatch.setattr(runner.restic, "repo_exists", lambda *a, **k: False)
        client = Recorder()
        assert run(client=client, do_init=True) is True
        assert len(env.restic_calls("init_repo")) == 1 and "send_backup_stage" in client.names()

    def test_no_password_stops_before_anything_happens(self, env):
        env.monkeypatch.delenv("RESTIC_PASSWORD")
        client = Recorder()
        assert run(client=client) is False
        assert client.calls == [] and state.read() == {}

    def test_a_job_that_is_already_running_is_skipped_quietly(self, env):
        held = locking.acquire_lock(runner.lock_path("jim-home"))
        try:
            client = Recorder()
            assert run(client=client) is False
            assert client.calls == [] and env.restic_calls("backup") == []
            assert state.read() == {}  # a skipped run is not a failed run
        finally:
            locking.release_lock(held)

    def test_dashboard_trouble_never_changes_the_outcome(self, env):
        class Down(Recorder):
            def __getattr__(self, name):
                return lambda *a, **k: False
        assert run(client=Down()) is True


def test_lock_names_are_filesystem_safe(home):
    assert runner.lock_path("my job/../x").endswith("my_job____x.lock")


def test_progress_is_logged_per_decile_and_posted_at_most_every_few_seconds(env, caplog):
    posted = []

    def fake_backup(*args, **kwargs):
        for pct in (0.05, 0.12, 0.13, 0.55, 0.99):
            kwargs["on_progress"]({"message_type": "status", "percent_done": pct, "seconds_remaining": 5, "bytes_done": 10})
        kwargs["on_progress"]({"message_type": "status"})  # no percent: ignored
        return SUMMARY
    env.monkeypatch.setattr(runner.restic, "backup", fake_backup)
    client = Recorder()
    env.monkeypatch.setattr(client, "send_backup_progress", lambda *a, **k: posted.append(k["percent_complete"]) or True, raising=False)
    with caplog.at_level("INFO", logger="app.runner"):
        run(client=client)
    assert posted == [5]  # the rest fell inside the five-second window
    progress_lines = [r.getMessage() for r in caplog.records if "Progress:" in r.getMessage()]
    assert len(progress_lines) == 4  # deciles 0, 1, 5 and 9
