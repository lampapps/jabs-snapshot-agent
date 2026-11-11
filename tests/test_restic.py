import json
import subprocess
from types import SimpleNamespace

import pytest

from app import restic
from app.config import ResticOptions
from app.restic import ResticError


def completed(stdout=""):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


@pytest.fixture
def run(monkeypatch):
    """Capture the restic command line instead of running restic."""
    calls = []

    def fake(cmd, **kwargs):
        calls.append(SimpleNamespace(cmd=cmd, env=kwargs["env"]))
        return completed(fake.stdout)
    fake.stdout = ""
    monkeypatch.setattr(restic.subprocess, "run", fake)
    fake.calls = calls
    return fake


class TestParseJson:
    def test_single_value(self):
        assert restic.parse_json_output('{"a": 1}') == {"a": 1}

    def test_newline_delimited(self):
        assert restic.parse_json_output('{"a": 1}\n{"b": 2}\n') == [{"a": 1}, {"b": 2}]

    @pytest.mark.parametrize("text", ["", "   \n  ", "warning: nothing JSON here"])
    def test_empty(self, text):
        assert restic.parse_json_output(text) is None

    def test_plain_text_warnings_between_json_lines_are_skipped(self):
        text = 'warning: running prune without a cache, this may be very slow!\n{"remove": []}\n'
        assert restic.parse_json_output(text) == {"remove": []}


class TestRunRestic:
    def test_failure_carries_stderr(self, monkeypatch):
        def boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="repository not found")
        monkeypatch.setattr(restic.subprocess, "run", boom)
        with pytest.raises(ResticError, match="repository not found"):
            restic.run_restic(["snapshots"], "/tmp/repo")

    def test_missing_binary(self, monkeypatch):
        def missing(cmd, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr(restic.subprocess, "run", missing)
        with pytest.raises(ResticError, match="not found"):
            restic.run_restic(["snapshots"], "/tmp/repo")

    def test_password_file_wins_and_nothing_secret_is_in_the_arguments(self, run):
        restic.run_restic(["snapshots"], "/tmp/repo", password="secret", password_file="/tmp/pw")
        call = run.calls[0]
        assert call.env["RESTIC_PASSWORD_FILE"] == "/tmp/pw" and "RESTIC_PASSWORD" not in call.env
        assert call.env["RESTIC_REPOSITORY"] == "/tmp/repo"
        assert "secret" not in " ".join(call.cmd) and "/tmp/repo" not in " ".join(call.cmd)

    def test_binary_and_global_flags(self, run, monkeypatch):
        monkeypatch.setenv("RESTIC_BIN", "/opt/restic")
        restic.run_restic(["check"], "/r", global_args=["--limit-upload", "5"])
        assert run.calls[0].cmd == ["/opt/restic", "--limit-upload", "5", "check"]


def test_build_global_args():
    assert restic.build_global_args(limit_upload=10, compression="auto", pack_size=8, retry_lock="5m") == [
        "--limit-upload", "10", "--compression", "auto", "--pack-size", "8", "--retry-lock", "5m"]
    assert restic.build_global_args(no_cache=True, cache_dir="/c") == ["--no-cache"]
    assert restic.build_global_args() == []


def test_options_helpers():
    options = ResticOptions(limit_upload=5, compression="max", host="h", read_concurrency=3, exclude_caches=False)
    assert restic.global_args_from(options) == ["--limit-upload", "5", "--compression", "max"]
    kwargs = restic.backup_kwargs_from(options)
    assert (kwargs["host"], kwargs["read_concurrency"], kwargs["exclude_caches"]) == ("h", 3, False)
    assert restic.backup_kwargs_from(ResticOptions())["host"] is None


class TestBackup:
    def test_parses_the_summary_and_builds_arguments(self, run):
        run.stdout = "\n".join(json.dumps(e) for e in [
            {"message_type": "status", "percent_done": 0.5},
            {"message_type": "summary", "snapshot_id": "abc123", "files_new": 3, "data_added": 1024}])
        summary = restic.backup("/r", ["/home/jim"], tags=["job1"], exclude_patterns=["*.tmp"], host="H",
                                one_file_system=True, exclude_larger_than="1G")
        assert (summary["snapshot_id"], summary["files_new"]) == ("abc123", 3)
        cmd = run.calls[0].cmd
        for expected in ("--tag", "job1", "--exclude", "*.tmp", "--host", "H", "--one-file-system", "1G", "/home/jim"):
            assert expected in cmd

    def test_no_summary_is_an_error(self, run):
        run.stdout = json.dumps({"message_type": "status", "percent_done": 0.1})
        with pytest.raises(ResticError, match="no summary"):
            restic.backup("/r", ["/home/jim"])


class TestForgetPrune:
    def test_flags_and_removed_ids(self, run):
        run.stdout = json.dumps([{"keep": [], "remove": [{"id": "a" * 64}, {"id": "b" * 64}]}, {"remove": None}])
        removed = restic.forget_prune("/r", keep_daily=7, keep_weekly=4, keep_monthly=0, tags=["job1"],
                                      group_by="host", keep_within="30d")
        cmd = run.calls[0].cmd
        assert removed == ["a" * 64, "b" * 64]
        assert "--prune" in cmd and "--keep-monthly" not in cmd and "--keep-within" in cmd
        assert cmd[cmd.index("--keep-daily") + 1] == "7"

    def test_nothing_removed(self, run):
        run.stdout = ""
        assert restic.forget_prune("/r", keep_last=1) == []


def test_restore_arguments(run):
    restic.restore("/r", "abc123", "/tmp/dest", include_path="/data/file.txt")
    cmd = run.calls[0].cmd
    assert cmd[:2] == ["restic", "restore"] and "--target" in cmd and "--include" in cmd


class TestCheck:
    def test_metadata_only_by_default(self, run):
        restic.check("/r")
        assert run.calls[0].cmd == ["restic", "check"]

    def test_deep_and_subset(self, run):
        restic.check("/r", read_data=True)
        restic.check("/r", read_data=True, read_data_subset="5%")
        assert run.calls[0].cmd[-1] == "--read-data" and run.calls[1].cmd[-1] == "--read-data-subset=5%"


class TestStats:
    def test_repository_size(self, run):
        run.stdout = json.dumps({"total_size": 5, "snapshots_count": 2})
        assert restic.stats("/r", mode="raw-data") == {"total_size": 5, "snapshots_count": 2}
        assert run.calls[0].cmd == ["restic", "stats", "--json", "--mode", "raw-data"]

    def test_latest_snapshot_of_a_job(self, run):
        run.stdout = json.dumps({"total_size": 9})
        restic.stats("/r", snapshot="latest", tags=["a", "b"], host="H", paths=["/d1", "/d2"])
        assert run.calls[0].cmd == ["restic", "stats", "--json", "--mode", "restore-size", "--tag", "a,b",
                                    "--host", "H", "--path", "/d1", "--path", "/d2", "latest"]

    def test_empty_output_is_an_empty_dict(self, run):
        run.stdout = ""
        assert restic.stats("/r") == {}
