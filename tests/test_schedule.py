from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import schedule, state

UTC = timezone.utc


def at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def job(name="j", *crons):
    return SimpleNamespace(job_id=name, name=name, enabled_crons=lambda: list(crons))


def plan(jobs, data, now):
    return [j.name for j in schedule.due_jobs(jobs, data, now)]


class TestSlots:
    def test_the_latest_slot_includes_the_current_minute(self):
        assert schedule.latest_slot("0 3 * * *", at(2027, 1, 15, 3, 0, 0)) == at(2027, 1, 15, 3, 0)
        assert schedule.latest_slot("0 3 * * *", at(2027, 1, 15, 3, 4, 59)) == at(2027, 1, 15, 3, 0)
        assert schedule.latest_slot("0 3 * * *", at(2027, 1, 15, 2, 59, 59)) == at(2027, 1, 14, 3, 0)

    def test_next_slot_is_strictly_later(self):
        assert schedule.next_slot("0 3 * * *", at(2027, 1, 15, 3, 0)) == at(2027, 1, 16, 3, 0)

    def test_next_run_is_the_soonest_enabled_schedule(self):
        j = job("j", "0 3 * * *", "30 1 * * *")
        assert schedule.next_run(j, at(2027, 1, 15, 0, 0)) == at(2027, 1, 15, 1, 30)
        assert schedule.next_run(job("none"), at(2027, 1, 15)) is None


class TestDue:
    def test_first_sight_only_sets_a_baseline(self):
        data: dict = {}
        assert plan([job("j", "0 3 * * *")], data, at(2027, 1, 15, 8)) == []
        assert data["jobs"]["j"]["served"] == {"0 3 * * *": at(2027, 1, 15, 3).timestamp()}

    def test_a_new_slot_is_due_once(self):
        data: dict = {}
        j = job("j", "0 3 * * *")
        plan([j], data, at(2027, 1, 15, 2, 55))
        assert plan([j], data, at(2027, 1, 15, 3, 0, 30)) == ["j"]
        assert plan([j], data, at(2027, 1, 15, 3, 5)) == []
        assert plan([j], data, at(2027, 1, 15, 23)) == []

    def test_a_tick_that_fires_late_still_catches_the_slot(self):
        data: dict = {}
        j = job("j", "0 3 * * *")
        plan([j], data, at(2027, 1, 15, 2, 59))
        assert plan([j], data, at(2027, 1, 15, 3, 4, 59)) == ["j"]

    def test_several_missed_slots_run_once(self):
        data: dict = {}
        j = job("j", "0 * * * *")
        plan([j], data, at(2027, 1, 15, 0, 30))
        assert plan([j], data, at(2027, 1, 15, 9, 10)) == ["j"]  # eight hourly slots missed
        assert plan([j], data, at(2027, 1, 15, 9, 15)) == []

    def test_two_due_schedules_run_the_job_once(self):
        data: dict = {}
        j = job("j", "0 3 * * *", "0 * * * *")
        plan([j], data, at(2027, 1, 15, 2, 30))
        assert plan([j], data, at(2027, 1, 15, 3, 1)) == ["j"]

    def test_a_new_expression_gets_its_own_baseline(self):
        data: dict = {}
        plan([job("j", "0 3 * * *")], data, at(2027, 1, 15, 8))
        assert plan([job("j", "0 3 * * *", "0 6 * * *")], data, at(2027, 1, 15, 8)) == []

    def test_jobs_without_enabled_schedules_are_never_due(self):
        data: dict = {}
        plan([job("j")], data, at(2027, 1, 15, 8))
        assert plan([job("j")], data, at(2027, 1, 16, 8)) == []

    def test_jobs_are_independent(self):
        data: dict = {}
        a, b = job("a", "0 3 * * *"), job("b", "0 4 * * *")
        plan([a, b], data, at(2027, 1, 15, 2, 50))
        assert plan([a, b], data, at(2027, 1, 15, 3, 1)) == ["a"]
        assert plan([a, b], data, at(2027, 1, 15, 4, 1)) == ["b"]

    def test_daylight_saving_does_not_skip_or_repeat(self):
        zone = pytest.importorskip("zoneinfo").ZoneInfo("America/New_York")
        data: dict = {}
        j = job("j", "30 2 * * *")  # a time that does not exist on the spring-forward day
        start = datetime(2027, 3, 13, 12, tzinfo=zone)
        plan([j], data, start)
        runs = 0
        for hours in range(1, 72):
            runs += len(plan([j], data, start + timedelta(hours=hours)))
        assert runs == 3  # the nights of Mar 14 (spring forward), 15 and 16: one run each


class TestState:
    def test_transaction_writes_on_success(self, home):
        with state.transaction() as data:
            data["a"] = 1
        assert state.read() == {"a": 1}

    def test_nothing_is_written_when_the_block_fails(self, home):
        with state.transaction() as data:
            data["a"] = 1
        with pytest.raises(RuntimeError):
            with state.transaction() as data:
                data["a"] = 2
                raise RuntimeError("boom")
        assert state.read() == {"a": 1}

    def test_missing_and_corrupt_files_read_as_empty(self, home):
        assert state.read() == {}
        home.joinpath("data").mkdir()
        (home / "data" / "state.json").write_text("{not json")
        assert state.read() == {}
        (home / "data" / "state.json").write_text("[1, 2]")
        assert state.read() == {}

    def test_record_run_keeps_the_latest_result_per_job(self, home):
        state.record_run("j", {"success": True, "finished_at": 1})
        state.record_run("j", {"success": False, "finished_at": 2})
        state.record_run("k", {"success": True, "finished_at": 3})
        jobs = state.read()["jobs"]
        assert jobs["j"]["last_run"]["finished_at"] == 2 and jobs["k"]["last_run"]["success"] is True
