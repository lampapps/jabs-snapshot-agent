"""End-to-end against the real restic binary (skipped when restic is not installed)."""

import json

import pytest

from app import __main__ as cli
from app import restic, state, storage
from app.config import Config, find_job, load_config

from .conftest import requires_restic, write_config, write_job

pytestmark = requires_restic


@pytest.fixture
def repo(home, tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "report.txt").write_text("hello restic\n")
    (source / "big.bin").write_bytes(b"x" * 50_000)
    repo_dir = tmp_path / "repo"
    write_config(home, f"repo_path: {repo_dir}\nrestic_options: {{cache_dir: {tmp_path / 'cache'}}}\nretention: {{keep_last: 1}}\n")
    write_job(home, "docs", f'job_name: docs\npaths: ["{source}"]\ntags: [docs]\nprune_after_backup: true\n')
    monkeypatch.setenv("RESTIC_PASSWORD", "integration-test")
    monkeypatch.setattr(cli.settings, "load_env", lambda: None)

    class Repo:
        pass
    r = Repo()
    r.dir, r.source = repo_dir, source
    return r


def test_run_creates_backs_up_prunes_and_records(repo, capsys):
    assert cli.main(["run", "docs", "--init"]) == 0
    first = state.read()["jobs"]["docs"]["last_run"]
    assert first["success"] and len(first["snapshot_id"]) == 64 and first["files"] >= 2

    (repo.source / "new.txt").write_text("changed\n")
    assert cli.main(["run", "docs"]) == 0  # keep_last: 1 prunes the first snapshot
    cfg = load_config()
    snaps = restic.list_snapshots(str(repo.dir), tags=["docs"], password="integration-test",
                                  global_args=restic.global_args_from(cfg.restic_options))
    assert len(snaps) == 1 and snaps[0]["id"] != first["snapshot_id"]


def test_a_missing_repository_needs_init(repo, capsys):
    assert cli.main(["run", "docs"]) == 1
    assert state.read()["jobs"]["docs"]["last_run"]["success"] is False


def test_storage_samples_come_from_real_numbers(repo):
    assert cli.main(["run", "docs", "--init"]) == 0
    cfg = load_config()
    job = find_job("docs", cfg)
    volume = storage.volume_sample(str(repo.dir))
    assert volume["total_bytes"] >= volume["used_bytes"] > 0
    size = storage.repo_size_sample(cfg, str(repo.dir))
    assert size["kind"] == "repo" and size["used_bytes"] > 0
    source = storage.source_size_sample(cfg, job)
    assert source["name"] == "docs" and source["series"] == "source" and source["used_bytes"] >= 50_000


def test_the_source_size_ignores_other_jobs_snapshots(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "tiny.txt").write_text("x")
    assert cli.main(["run", "docs", "--init"]) == 0
    cfg = load_config()
    restic.backup(str(repo.dir), [str(other)], tags=["other"], password="integration-test",
                  global_args=restic.global_args_from(cfg.restic_options))
    docs = storage.source_size_sample(cfg, find_job("docs", cfg))
    assert docs["used_bytes"] >= 50_000  # still the docs snapshot, not the newer "other" one


def test_a_job_with_no_snapshot_yet_has_no_source_sample(repo):
    cfg = load_config()
    assert cli.main(["init-repo"]) == 0
    assert storage.source_size_sample(cfg, find_job("docs", cfg)) is None


def test_snapshots_ls_find_and_restore(repo, tmp_path, capsys):
    assert cli.main(["run", "docs", "--init"]) == 0
    capsys.readouterr()
    assert cli.main(["snapshots", "--job", "docs"]) == 0
    assert "docs" in capsys.readouterr().out
    snapshot = json.loads(json.dumps(restic.list_snapshots(str(repo.dir), password="integration-test",
                                                           global_args=["--no-cache"])))[0]["short_id"]
    assert cli.main(["ls", snapshot]) == 0
    assert "report.txt" in capsys.readouterr().out
    assert cli.main(["find", "REPORT"]) == 0  # partial and case-insensitive
    assert "report.txt" in capsys.readouterr().out
    target = tmp_path / "restored"
    assert cli.main(["restore", snapshot, str(target), "--path", f"{repo.source}/docs/report.txt"]) == 0
    assert (target / str(repo.source).lstrip("/") / "docs" / "report.txt").read_text() == "hello restic\n"


def test_checks_pass_on_a_healthy_repository(repo):
    assert cli.main(["run", "docs", "--init"]) == 0
    assert cli.main(["check"]) == 0
    assert cli.main(["check", "--deep"]) == 0
    assert cli.main(["check", "--subset", "50%"]) == 0


def test_dry_run_leaves_no_snapshot(repo):
    assert cli.main(["init-repo"]) == 0
    assert cli.main(["run", "docs", "--dry-run"]) == 0
    assert restic.list_snapshots(str(repo.dir), password="integration-test", global_args=["--no-cache"]) == []


def test_snapshot_host_follows_restic_options(repo, home):
    write_config(home, f"repo_path: {repo.dir}\nrestic_options: {{cache_dir: {repo.dir.parent / 'cache'}, host: custom-host}}\n")
    assert cli.main(["run", "docs", "--init"]) == 0
    snaps = restic.list_snapshots(str(repo.dir), password="integration-test", global_args=["--no-cache"])
    assert {s["hostname"] for s in snaps} == {"custom-host"}
    cfg = Config(repo_path=str(repo.dir))
    assert cfg.restic_options.host == ""
