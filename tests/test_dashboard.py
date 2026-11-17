import logging
from types import SimpleNamespace

import pytest
import requests

from app import dashboard
from app.config import Config
from app.dashboard import Client


class Posts:
    """Stands in for requests.post and records what was sent."""

    def __init__(self, status=201, error=None):
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, json=json, headers=headers, timeout=timeout))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text="body")


@pytest.fixture
def posts(monkeypatch):
    recorder = Posts()
    monkeypatch.setattr(dashboard.requests, "post", recorder)
    return recorder


def live(hostname="P3Tiny") -> Client:
    return Client("http://dash:5002/", "key123", hostname)


class TestMode:
    def test_standalone_without_url_or_key(self, monkeypatch):
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash:5002")
        assert not Client.from_env(Config()).enabled  # a URL without a key is not enough
        monkeypatch.delenv("JABS_DASHBOARD_URL")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        assert not Client.from_env(Config()).enabled

    def test_enabled_with_both_and_trailing_slash_removed(self, monkeypatch):
        monkeypatch.setenv("JABS_DASHBOARD_URL", " http://dash:5002/ ")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        client = Client.from_env(Config())
        assert client.enabled and client.url == "http://dash:5002"

    def test_the_config_can_force_standalone(self, monkeypatch):
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        client = Client.from_env(Config(dashboard={"enabled": False}))
        assert not client.enabled and "standalone" in client.describe()

    def test_hostname_comes_from_config_or_the_system(self, monkeypatch):
        monkeypatch.setattr("app.settings.socket.gethostname", lambda: "sysname")
        assert Client.from_env(Config()).hostname == "sysname"
        assert Client.from_env(Config(hostname="P3Tiny")).hostname == "P3Tiny"

    def test_standalone_never_touches_the_network(self, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("a standalone agent must not make requests")
        monkeypatch.setattr(dashboard.requests, "post", forbidden)
        client = Client(hostname="h")
        assert client.send_event("heartbeat", "x") is False
        assert client.send_backup_start("j", "snapshot", "j", "j") is False
        assert client.send_scheduler_check() is False
        assert client.send_job_schedule("j", "0 3 * * *") is False
        assert client.send_target_purged("j", ["abc"]) is False
        assert client.post_storage([{"name": "n"}]) is False
        assert client.send_jobs([{"job_name": "j", "targets": []}]) is False


class TestJobs:
    def test_the_jobs_go_to_the_v2_endpoint_with_the_hostname(self, posts):
        jobs = [{"job_name": "j", "targets": [{"name": "j", "source": "/data", "destination": "/repo"}]}]
        assert live("P3Tiny").send_jobs(jobs) is True
        call = posts.calls[0]
        assert call.url == "http://dash:5002/api/v2/jobs" and call.headers == {"X-API-Key": "key123"}
        assert call.json == {"hostname": "P3Tiny", "jobs": jobs}

    def test_an_old_dashboard_without_the_endpoint_is_not_a_warning(self, posts, caplog):
        posts.status = 404
        with caplog.at_level(logging.DEBUG):
            assert live().send_jobs([]) is False
        assert [r.levelno for r in caplog.records if "/api/v2/jobs" in r.getMessage()] == [logging.DEBUG]

    def test_any_other_rejection_is_still_a_warning(self, posts, caplog):
        posts.status = 400
        with caplog.at_level(logging.DEBUG):
            live().send_jobs([])
        assert [r.levelno for r in caplog.records if "/api/v2/jobs" in r.getMessage()] == [logging.WARNING]

    def test_the_payload_has_the_paths_as_source_and_the_repository_as_destination(self):
        job = SimpleNamespace(name="jim-home", paths=["/home/jim", "/etc"], repo_path="s3:s3.amazonaws.com/b/jim",
                              schedules=[SimpleNamespace(cron="0 0 * * *", enabled=True),
                                         SimpleNamespace(cron="0 9 * * 1", enabled=False)])
        assert dashboard.jobs_payload([job]) == [{"job_name": "jim-home", "schedules": [
            {"cron": "0 0 * * *", "enabled": True}, {"cron": "0 9 * * 1", "enabled": False}], "targets": [{
                "name": "jim-home", "source": "/home/jim\n/etc", "destination": "s3:s3.amazonaws.com/b/jim"}]}]

    @pytest.mark.parametrize("repo,shown", [
        ("rest:https://jim:s3cret@backup.lan:8000/jim", "rest:https://jim@backup.lan:8000/jim"),
        ("rest:http://:pw@host/repo", "rest:http://@host/repo"),
        ("s3:s3.amazonaws.com/bucket/jim", "s3:s3.amazonaws.com/bucket/jim"),
        ("sftp:jim@nas:/srv/restic", "sftp:jim@nas:/srv/restic"),
        ("/srv/restic/repo", "/srv/restic/repo"),
    ])
    def test_a_password_in_the_repository_url_never_leaves_the_agent(self, repo, shown):
        job = SimpleNamespace(name="j", paths=["/d"], repo_path=repo, schedules=[])
        (entry,) = dashboard.jobs_payload([job])
        assert entry["targets"][0]["destination"] == shown
        assert "s3cret" not in str(entry) and ":pw@" not in str(entry)


class TestEvents:
    def test_every_event_carries_the_hostname_version_and_type(self, posts):
        assert live("P3Tiny").send_event("heartbeat", "hi", job_name="j") is True
        call = posts.calls[0]
        assert call.url == "http://dash:5002/api/monitoring/events"
        assert call.headers == {"X-API-Key": "key123"}
        assert call.json["hostname"] == "P3Tiny" and call.json["agent_type"] == "Snapshot"
        assert call.json["version"] and call.json["event_type"] == "heartbeat" and call.json["job_name"] == "j"
        assert "run_id" not in call.json  # absent values are not sent

    def test_backup_lifecycle_payloads(self, posts):
        client = live()
        client.send_backup_start("j", "snapshot", "j", "j", run_id="r1")
        client.send_backup_progress("j", "snapshot", "j", run_id="r1", percent_complete=40, eta_seconds=9)
        client.send_backup_complete("j", "snapshot", "j", "j", 12.5, run_id="r1", files_backed_up=3,
                                    bytes_backed_up=99, snapshot_id="a" * 64, snapshot_short_id="aaaaaaaa")
        client.send_backup_complete("j", "snapshot", "j", "j", 1.0, run_id="r2", success=False, error_message="boom")
        start, progress, done, failed = (c.json for c in posts.calls)
        assert start["stage"] == "Starting backup" and start["target_id"] == "j"
        assert progress["percent_complete"] == 40 and "duration_seconds" not in progress
        assert (done["event_type"], done["status"], done["external_id"], done["duration_seconds"]) == (
            "backup_complete", "success", "a" * 64, 12.5)
        assert (failed["event_type"], failed["status"], failed["error_message"]) == ("error", "failed", "boom")

    def test_dry_runs_are_labelled(self, posts):
        live().send_backup_stage("j", "snapshot", "j", "j", "Running backup", dry_run=True)
        assert posts.calls[0].json["message"].startswith("[DRY RUN]")

    def test_check_results(self, posts):
        live().send_check_result("j", "snapshot", "j", "j", "r1", success=False, error_message="bad pack")
        assert posts.calls[0].json["event_type"] == "error" and posts.calls[0].json["error_message"] == "bad pack"

    def test_schedule_heartbeat_has_no_target(self, posts):
        live().send_job_schedule("j", "0 3 * * *,0 9 * * *")
        body = posts.calls[0].json
        assert body["cron_schedule"] == "0 3 * * *,0 9 * * *" and body["job_name"] == "j" and "target_id" not in body

    def test_target_purged(self, posts):
        assert live().send_target_purged("j", []) is False and posts.calls == []
        posts.status = 200
        assert live().send_target_purged("j", ["a", "b"]) is True
        assert posts.calls[0].url.endswith("/api/monitoring/target-purged")
        assert posts.calls[0].json["external_ids"] == ["a", "b"]

    def test_a_rejection_is_reported_as_false(self, posts):
        posts.status = 403
        assert live().send_event("heartbeat", "x") is False


class TestFailures:
    def test_a_connection_error_pauses_the_dashboard_briefly(self, monkeypatch):
        recorder = Posts(error=requests.exceptions.ConnectionError("down"))
        monkeypatch.setattr(dashboard.requests, "post", recorder)
        clock = [1000.0]
        monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
        client = live()
        assert client.send_event("heartbeat", "x") is False
        assert client.send_event("heartbeat", "x") is False
        assert len(recorder.calls) == 1  # the second call was skipped during the cool-down
        clock[0] += dashboard.COOLDOWN_S + 1
        client.send_event("heartbeat", "x")
        assert len(recorder.calls) == 2

    def test_http_errors_do_not_trigger_the_cool_down(self, posts):
        posts.status = 500
        client = live()
        client.send_event("heartbeat", "x")
        client.send_event("heartbeat", "x")
        assert len(posts.calls) == 2

    def test_dashboard_logging_never_raises_into_the_caller(self, monkeypatch, caplog):
        monkeypatch.setattr(dashboard.requests, "post", Posts(status=400))
        with caplog.at_level(logging.WARNING, logger="app.dashboard"):
            assert live().send_event("heartbeat", "x") is False


class TestStorage:
    def test_storage_payload(self, posts):
        sample = {"name": "Restic repo", "kind": "repo", "used_bytes": 5}
        assert live("NAS1").post_storage([sample]) is True
        assert posts.calls[0].url.endswith("/api/v2/storage")
        assert posts.calls[0].json == {"hostname": "NAS1", "samples": [sample]}
        assert live().post_storage([]) is False
