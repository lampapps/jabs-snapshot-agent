import pytest

from app import state, storage
from app.config import Config, Job, ResticOptions
from app.restic import ResticError


def make_job(name="jim-home", tags=("jim-home",), repo="/mnt/nas/repo", paths=("/data",), host=""):
    return Job(name=name, path="x.yaml", repo_path=repo, paths=list(paths), tags=list(tags), schedules=[],
               prune_after_backup=False, verify_after_backup=True, retention={}, restic_options=ResticOptions(host=host),
               exclude=[])


class TestVolume:
    def test_local_repository_reports_used_and_total(self, tmp_path):
        sample = storage.volume_sample(str(tmp_path))
        assert sample["kind"] == "local" and sample["name"] == "Repo volume"
        assert 0 < sample["used_bytes"] <= sample["total_bytes"]

    @pytest.mark.parametrize("repo", ["sftp:me@nas:/backups", "s3:s3.amazonaws.com/bucket", "rest:http://h:8000/",
                                      "relative/repo", "/does/not/exist"])
    def test_remote_or_missing_repositories_have_no_volume(self, repo):
        assert storage.volume_sample(repo) is None

    def test_is_local_repo(self):
        assert storage.is_local_repo("/mnt/x") and not storage.is_local_repo("sftp:/x")


class TestRepoSize:
    def test_uses_raw_data_mode(self, monkeypatch):
        seen = {}

        def fake(repo, **kwargs):
            seen.update(repo=repo, **kwargs)
            return {"total_size": 4096, "snapshots_count": 3}
        monkeypatch.setattr(storage.restic, "stats", fake)
        monkeypatch.setenv("RESTIC_PASSWORD", "pw")
        sample = storage.repo_size_sample(Config(), "/r")
        assert sample == {"name": "Restic repo", "kind": "repo", "used_bytes": 4096}
        assert seen["mode"] == "raw-data" and seen["password"] == "pw"

    def test_restic_errors_become_no_sample(self, monkeypatch):
        def boom(*a, **k):
            raise ResticError("locked")
        monkeypatch.setattr(storage.restic, "stats", boom)
        assert storage.repo_size_sample(Config(), "/r") is None


class TestSourceSize:
    def test_latest_snapshot_selected_by_tags_and_host(self, monkeypatch):
        seen = {}

        def fake(repo, **kwargs):
            seen.update(kwargs)
            return {"total_size": 777, "snapshots_count": 1}
        monkeypatch.setattr(storage.restic, "stats", fake)
        sample = storage.source_size_sample(Config(), make_job(host="H1"))
        assert sample == {"name": "jim-home", "series": "source", "kind": "repo", "used_bytes": 777}
        assert (seen["snapshot"], seen["tags"], seen["host"], seen["paths"], seen["mode"]) == (
            "latest", ["jim-home"], "H1", None, "restore-size")

    def test_untagged_jobs_are_selected_by_path(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(storage.restic, "stats", lambda repo, **kw: seen.update(kw) or {"total_size": 1, "snapshots_count": 1})
        storage.source_size_sample(Config(), make_job(tags=(), paths=("/a", "/b")))
        assert seen["tags"] is None and seen["paths"] == ["/a", "/b"]

    def test_no_matching_snapshot_is_no_sample(self, monkeypatch):
        monkeypatch.setattr(storage.restic, "stats", lambda *a, **k: {"total_size": 0, "snapshots_count": 0})
        assert storage.source_size_sample(Config(), make_job()) is None

    def test_errors_are_swallowed(self, monkeypatch):
        def boom(*a, **k):
            raise ResticError("nope")
        monkeypatch.setattr(storage.restic, "stats", boom)
        assert storage.source_size_sample(Config(), make_job()) is None


class TestScheduled:
    @pytest.fixture(autouse=True)
    def fakes(self, home, monkeypatch, tmp_path):
        self.repo = str(tmp_path)
        monkeypatch.setattr(storage, "repo_size_sample", lambda cfg, path: {"name": "Restic repo", "kind": "repo", "used_bytes": 1})

    def names(self, samples):
        return sorted(s["name"] for s in samples)

    def test_everything_is_due_the_first_time(self):
        assert self.names(storage.collect_scheduled(Config(), [self.repo], now=1000.0)) == ["Repo volume", "Restic repo"]

    def test_volume_and_repo_have_their_own_intervals(self):
        cfg = Config()
        storage.collect_scheduled(cfg, [self.repo], now=1000.0)
        assert storage.collect_scheduled(cfg, [self.repo], now=1000.0 + 60) == []
        later = 1000.0 + 15 * 60
        assert self.names(storage.collect_scheduled(cfg, [self.repo], now=later)) == ["Repo volume"]
        much_later = 1000.0 + 12 * 3600
        assert self.names(storage.collect_scheduled(cfg, [self.repo], now=much_later)) == ["Repo volume", "Restic repo"]

    def test_force_ignores_the_throttle(self):
        cfg = Config()
        storage.collect_scheduled(cfg, [self.repo], now=1000.0)
        assert len(storage.collect_scheduled(cfg, [self.repo], now=1001.0, force=True)) == 2

    def test_throttle_times_are_kept_in_the_state_file(self):
        storage.collect_scheduled(Config(), [self.repo], now=1000.0)
        assert state.read()["storage"] == {"volume": 1000.0, "repo_size": 1000.0}

    def test_remote_repositories_only_report_their_size(self):
        assert self.names(storage.collect_scheduled(Config(), ["sftp:me@nas:/r"], now=1.0)) == ["Restic repo"]


def test_describe():
    text = storage.describe([
        {"name": "Repo volume", "kind": "local", "used_bytes": 5, "total_bytes": 10},
        {"name": "jim-home", "series": "source", "kind": "repo", "used_bytes": 1234}])
    assert "Repo volume: 5 of 10 bytes" in text and "jim-home (source): 1,234 bytes" in text
    assert storage.describe([]) == "nothing to report"
