from datetime import datetime, timezone

import pytest

from app import config, runner, schedule, state, storage, tick
from app.config import Config

from .conftest import write_config, write_job
from .test_runner import Recorder

UTC = timezone.utc
JOB = 'job_name: "{name}"\npaths: [/data]\nschedules:\n  - cron: "{cron}"\n    enabled: true\n'


def at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def jobs(home):
    write_config(home)
    write_job(home, "daily", JOB.format(name="daily", cron="0 3 * * *"))
    write_job(home, "hourly", JOB.format(name="hourly", cron="0 * * * *"))
    write_job(home, "manual", "job_name: manual\npaths: [/data]\n")
    return home


@pytest.fixture
def ran(monkeypatch):
    names = []
    monkeypatch.setattr(runner, "run_job", lambda job, cfg, client, **kw: names.append(job.name) or True)
    return names


def run_tick(client=None, now=None, cfg=None):
    tick.run_once(cfg or config.load_config(), client or Recorder(), now)


def test_the_first_tick_only_sets_baselines(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    assert ran == []
    assert client.first("send_scheduler_check")[1] == (0,)


def test_a_job_runs_when_its_slot_arrives(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(ran) == ["daily", "hourly"]  # the hourly slot at 03:00 and the daily one
    run_tick(now=at(2027, 1, 15, 3, 7))
    assert sorted(ran) == ["daily", "hourly"]  # nothing new


def test_a_missed_slot_runs_once_at_the_next_tick(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 9, 31))  # the machine was off all night
    assert sorted(ran) == ["daily", "hourly"]
    run_tick(now=at(2027, 1, 15, 9, 36))
    assert sorted(ran) == ["daily", "hourly"]


def test_the_heartbeat_reports_each_schedule(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    reported = {args for name, args, _ in client.calls if name == "send_job_schedule"}
    assert reported == {("daily", "0 3 * * *"), ("hourly", "0 * * * *")}  # manual-only jobs have none
    assert client.names()[0] == "send_scheduler_check"  # first, so it goes out even if a backup runs long


def test_the_heartbeat_reports_each_job_with_its_source_and_repository(jobs, ran):
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 8, 2))
    (call,) = [c for c in client.calls if c[0] == "send_jobs"]
    reported = {entry["job_name"]: entry["targets"] for entry in call[1][0]}
    assert sorted(reported) == ["daily", "hourly", "manual"]  # manual-only jobs too: it is the config, not the schedule
    assert reported["daily"] == [{"name": "daily", "source": "/data", "destination": "/tmp/repo"}]
    assert client.names().index("send_jobs") > client.names().index("send_scheduler_check")


def test_the_due_count_is_in_the_heartbeat(jobs, ran):
    run_tick(now=at(2027, 1, 15, 2, 58))
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 3, 2))
    assert client.first("send_scheduler_check")[1] == (2,)


def test_a_crashing_job_does_not_stop_the_others(jobs, monkeypatch):
    seen = []

    def run_job(job, cfg, client, **kwargs):
        seen.append(job.name)
        if job.name == "daily":
            raise RuntimeError("boom")
        return True
    monkeypatch.setattr(runner, "run_job", run_job)
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(seen) == ["daily", "hourly"]


def test_a_broken_job_file_is_skipped_and_the_rest_still_run(jobs, ran):
    write_job(home := jobs, "broken", "paths: []\n")
    assert home
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    assert sorted(ran) == ["daily", "hourly"]


def test_a_failure_to_plan_still_sends_the_heartbeat(jobs, ran, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")
    monkeypatch.setattr(config, "load_jobs", broken)
    client = Recorder()
    run_tick(client, at(2027, 1, 15, 3, 2))
    assert client.first("send_scheduler_check")[1] == (0,) and ran == []


def test_a_job_that_became_due_while_another_ran_is_served_next_tick(jobs, ran, monkeypatch):
    """Planning marks slots served up front, so a long backup cannot make the next tick repeat them."""
    run_tick(now=at(2027, 1, 15, 2, 58))
    run_tick(now=at(2027, 1, 15, 3, 2))
    count = len(ran)
    run_tick(now=at(2027, 1, 15, 3, 3))
    assert len(ran) == count


class TestStorageTask:
    def test_standalone_measures_nothing(self, jobs, ran, monkeypatch):
        monkeypatch.setattr(storage, "collect_scheduled", lambda *a, **k: pytest.fail("must not measure"))
        run_tick(Recorder(enabled=False), at(2027, 1, 15, 8))

    def test_samples_go_to_the_dashboard_for_each_distinct_repo(self, jobs, ran, monkeypatch):
        write_job(jobs, "elsewhere", 'paths: [/d]\nrepo_path: /other\n')
        seen = {}
        monkeypatch.setattr(storage, "collect_scheduled", lambda cfg, paths, **k: seen.update(paths=paths) or [{"name": "n"}])
        client = Recorder()
        run_tick(client, at(2027, 1, 15, 8))
        assert seen["paths"] == ["/other", "/tmp/repo"]
        assert client.first("post_storage")[1] == ([{"name": "n"}],)

    def test_can_be_switched_off(self, jobs, ran, monkeypatch):
        monkeypatch.setattr(storage, "collect_scheduled", lambda *a, **k: pytest.fail("must not measure"))
        run_tick(Recorder(), at(2027, 1, 15, 8), Config(repo_path="/tmp/repo", storage={"report": False}))

    def test_a_storage_failure_does_not_fail_the_tick(self, jobs, ran, monkeypatch):
        def boom(*a, **k):
            raise OSError("nope")
        monkeypatch.setattr(storage, "collect_scheduled", boom)
        run_tick(Recorder(), at(2027, 1, 15, 8))  # must not raise


def test_a_corrupt_state_file_does_not_stop_a_tick(jobs, ran):
    (jobs / "data").mkdir(exist_ok=True)
    (jobs / "data" / "state.json").write_text("{broken")
    run_tick(now=at(2027, 1, 15, 8))
    assert state.read()["jobs"]["daily"]["served"]


def test_local_now_is_timezone_aware():
    assert schedule.local_now().tzinfo is not None
