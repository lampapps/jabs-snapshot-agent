import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import __main__ as cli
from app import mailer, restic, runner, state
from app.config import ConfigError

from .conftest import REPO_ROOT, write_config, write_job

JOB = 'job_name: "{name}"\npaths: [/data]\nschedules:\n  - cron: "{cron}"\n    enabled: true\n'


def main(*argv):
    return cli.main(list(argv))


@pytest.fixture(autouse=True)
def quiet_dotenv(monkeypatch):
    monkeypatch.setattr(cli.settings, "load_env", lambda: None)  # the real .env must never leak into tests


class TestConfigAndJobs:
    def test_config_summarises_the_install(self, home, capsys):
        write_config(home, "repo_path: /mnt/repo\n")
        write_job(home, "a", JOB.format(name="a", cron="0 3 * * *"))
        write_job(home, "b", "paths: [/d]\n")
        assert main("config") == 0
        out = capsys.readouterr().out
        assert "repo: /mnt/repo" in out and "standalone" in out and "a: 0 3 * * *" in out and "b: no schedule enabled" in out

    def test_config_reports_the_dashboard_when_configured(self, home, capsys, monkeypatch):
        write_config(home)
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash:5002/")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        assert main("config") == 0
        assert "reporting to http://dash:5002 as host" in capsys.readouterr().out

    def test_config_fails_on_a_broken_job(self, home, capsys):
        write_config(home)
        write_job(home, "bad", "paths: []\n")
        assert main("config") == 1
        assert "bad.yaml" in capsys.readouterr().err

    def test_a_missing_config_is_a_clean_error(self, home, capsys):
        assert main("config") == 1
        assert "config.yaml not found" in capsys.readouterr().err

    def test_jobs_lists_next_and_last_run(self, home, capsys):
        write_config(home)
        write_job(home, "a", JOB.format(name="a", cron="0 3 * * *"))
        write_job(home, "b", "paths: [/d]\n")
        state.record_run("a", {"success": False, "finished_at": 1_800_000_000})
        assert main("jobs") == 0
        lines = dict(line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines())
        assert "next 20" in lines["a"] and "last FAILED 2027-01-15" in lines["a"]
        assert lines["b"] == "next -\tlast -"


class TestTick:
    def test_standalone_tick_runs_without_a_dashboard(self, home, caplog):
        write_config(home)
        write_job(home, "a", JOB.format(name="a", cron="0 3 * * *"))
        with caplog.at_level("INFO"):
            assert main("tick") == 0
        assert "standalone mode" in caplog.text and "tick complete" in caplog.text
        assert state.read()["jobs"]["a"]["served"]  # baseline recorded

    def test_with_a_dashboard_logs_are_shipped_and_closed_cleanly(self, home, monkeypatch):
        write_config(home)
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        sent = []
        monkeypatch.setattr(cli.Client, "post_logs", lambda self, stream, lines, batch_id: sent.append((stream, lines)) or "ok")
        monkeypatch.setattr(cli.Client, "_post", lambda self, path, payload: None)
        monkeypatch.setattr(cli.storage, "collect_scheduled", lambda *a, **k: [])
        assert main("tick") == 0
        assert any(stream == "tick" and any("tick complete" in l["message"] for l in lines) for stream, lines in sent)
        assert not any(isinstance(h, cli.LogShipper) for h in logging.getLogger("app").handlers)


class TestRun:
    def test_unknown_job(self, home, capsys):
        write_config(home)
        assert main("run", "nope") == 1
        assert "not found" in capsys.readouterr().err

    def test_run_passes_the_flags(self, home, monkeypatch):
        write_config(home)
        write_job(home, "a", "paths: [/d]\n")
        seen = {}
        monkeypatch.setattr(runner, "run_job", lambda job, cfg, client, **kw: seen.update(kw, name=job.name) or True)
        assert main("run", "a", "--init", "--prune", "--dry-run") == 0
        assert seen == {"name": "a", "do_init": True, "do_prune": True, "dry_run": True}

    def test_exit_code_follows_the_job(self, home, monkeypatch):
        write_config(home)
        write_job(home, "a", "paths: [/d]\n")
        monkeypatch.setattr(runner, "run_job", lambda *a, **k: False)
        assert main("run", "a") == 1


class TestRepositoryCommands:
    @pytest.fixture(autouse=True)
    def setup(self, home, monkeypatch):
        write_config(home, "repo_path: /repo\n")
        monkeypatch.setenv("RESTIC_PASSWORD", "pw")

    def test_a_password_is_required(self, monkeypatch, capsys):
        monkeypatch.delenv("RESTIC_PASSWORD")
        assert main("check") == 1
        assert "RESTIC_PASSWORD" in capsys.readouterr().err

    def test_check_variants(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(restic, "check", lambda repo, pw, pf, read_data=False, read_data_subset=None, global_args=None:
                            calls.append((read_data, read_data_subset)))
        assert main("check") == 0 and main("check", "--deep") == 0 and main("check", "--subset", "5%") == 0
        assert calls == [(False, None), (True, None), (False, "5%")]

    def test_a_failed_check_exits_nonzero(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise restic.ResticError("unable to load pack")
        monkeypatch.setattr(restic, "check", boom)
        assert main("check") == 1
        assert "FAILED" in capsys.readouterr().err

    def test_init_repo(self, monkeypatch, capsys):
        created = []
        monkeypatch.setattr(restic, "repo_exists", lambda *a, **k: False)
        monkeypatch.setattr(restic, "init_repo", lambda *a, **k: created.append(a))
        assert main("init-repo") == 0 and len(created) == 1
        monkeypatch.setattr(restic, "repo_exists", lambda *a, **k: True)
        assert main("init-repo") == 0 and len(created) == 1
        assert "already exists" in capsys.readouterr().out

    def test_snapshots_for_a_job_filter_by_its_tags(self, home, monkeypatch, capsys):
        write_job(home, "a", "paths: [/d]\ntags: [t1]\n")
        seen = {}
        monkeypatch.setattr(restic, "list_snapshots", lambda repo, tags=None, **k: seen.update(tags=tags) or [
            {"short_id": "abcd1234", "time": "2027-01-15T03:00:00.1Z", "hostname": "P3Tiny", "tags": ["t1"], "paths": ["/d"]}])
        assert main("snapshots", "--job", "a") == 0
        assert seen["tags"] == ["t1"] and "abcd1234  2027-01-15 03:00:00  P3Tiny  t1  /d" in capsys.readouterr().out

    def test_ls_and_restore(self, monkeypatch, capsys):
        monkeypatch.setattr(restic, "list_files", lambda repo, snap, path=None, **k: [{"path": "/d/a"}, {"path": "/d/b"}, {"x": 1}])
        restored = []
        monkeypatch.setattr(restic, "restore", lambda repo, snap, target, include_path=None, **k: restored.append((snap, target, include_path)))
        assert main("ls", "abc") == 0 and capsys.readouterr().out.split() == ["/d/a", "/d/b"]
        assert main("restore", "abc", "/tmp/out", "--path", "/d/a") == 0
        assert restored == [("abc", "/tmp/out", "/d/a")]

    def test_find(self, monkeypatch, capsys):
        monkeypatch.setattr(restic, "find", lambda repo, pattern, **k: [
            {"matches": [{"path": "/d/report.txt"}, {"path": "/d/report.txt"}]}, {"matches": [{"path": "/e/report.pdf"}]}])
        assert main("find", "report") == 0
        out = capsys.readouterr().out
        assert "Searching repo: /repo" in out and "2 match(es)" in out and out.count("/d/report.txt") == 1
        monkeypatch.setattr(restic, "find", lambda *a, **k: [])
        assert main("find", "zzz") == 0 and "No matches" in capsys.readouterr().out


class TestMisc:
    def test_testmail(self, home, monkeypatch, capsys):
        write_config(home)
        assert main("testmail") == 1 and "not configured" in capsys.readouterr().err
        write_config(home, "repo_path: /r\nemail: {smtp_server: smtp.test, to_addrs: [a@example.com]}\n")
        sent = []
        monkeypatch.setattr(mailer, "send", lambda email, subject, body: sent.append(subject) or True)
        assert main("testmail") == 0 and sent == ["JABS snapshot agent test message"]
        monkeypatch.setattr(mailer, "send", lambda *a: False)
        assert main("testmail") == 1

    def test_reset_needs_confirmation_and_spares_the_config(self, home, capsys):
        write_config(home)
        (home / "data" / "locks").mkdir(parents=True)
        (home / "data" / "locks" / "a.lock").write_text("{}")
        state.record_run("a", {"success": True})
        assert main("reset") == 2 and (home / "data" / "state.json").exists()
        assert main("reset", "--yes") == 0
        assert not (home / "data" / "state.json").exists() and not (home / "data" / "locks").exists()
        assert (home / "config" / "config.yaml").exists()

    def test_paths_lists_what_the_service_may_write(self, home, capsys, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/home/svc"))
        write_config(home, "repo_path: /mnt/nas/repo\n")
        write_job(home, "a", "paths: [/d]\nrepo_path: sftp:me@host:/r\nrestic_options: {cache_dir: /var/cache/jabs}\n")
        assert main("paths") == 0
        lines = capsys.readouterr().out.split()
        assert str(home / "data") in lines and "/mnt/nas/repo" in lines and "/var/cache/jabs" in lines
        assert "/home/svc/.cache/restic" in lines and not any(l.startswith("sftp") for l in lines)

    def test_report_storage_prints_and_only_sends_when_there_is_a_dashboard(self, home, monkeypatch, capsys, tmp_path):
        write_config(home, f"repo_path: {tmp_path}\n")
        monkeypatch.setattr(cli.storage, "repo_size_sample", lambda cfg, path: {"name": "Restic repo", "kind": "repo", "used_bytes": 7})
        assert main("report-storage") == 0
        out = capsys.readouterr().out
        assert "Repo volume" in out and "Restic repo: 7 bytes" in out
        posted = []
        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        monkeypatch.setattr(cli.Client, "post_storage", lambda self, samples: posted.append(samples) or True)
        assert main("report-storage") == 0 and len(posted) == 1
        assert main("report-storage", "--print") == 0 and len(posted) == 1


class TestInstallScript:
    @pytest.fixture
    def app_dir(self, tmp_path):
        """A copy of the install layout whose python is the test interpreter's venv."""
        root = tmp_path / "agent"
        (root / "config" / "jobs").mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "deploy", root / "deploy")
        (root / "app").symlink_to(REPO_ROOT / "app")
        (root / ".venv").symlink_to(Path(sys.prefix))  # python finds its site-packages through pyvenv.cfg
        (root / "config" / "config.yaml").write_text("repo_path: /mnt/nas/repo\n")
        return root

    def render(self, app_dir, *args):
        out = app_dir / "out"
        result = subprocess.run(
            [str(app_dir / "deploy" / "install.sh"), *args, "--render-only", str(out)], capture_output=True, text=True,
            env={**os.environ, "HOME": str(app_dir)}, check=False)
        return result, out

    def test_units_are_rendered_for_the_chosen_user(self, app_dir):
        result, out = self.render(app_dir, "--user", "jim", "--name", "snap-test")
        assert result.returncode == 0, result.stderr
        service = (out / "snap-test.service").read_text()
        assert "User=jim" in service and f"ExecStart={app_dir}/.venv/bin/python -m app tick" in service
        assert "Type=oneshot" in service and "ProtectSystem=strict" in service and "NoNewPrivileges=yes" in service
        assert "@" not in service and "CAP_DAC_READ_SEARCH" not in service
        paths = next(l for l in service.splitlines() if l.startswith("ReadWritePaths=")).split("=", 1)[1].split()
        assert f"{app_dir}/data" in paths and "-/mnt/nas/repo" in paths
        timer = (out / "snap-test.timer").read_text()
        assert "OnCalendar=*:0/5" in timer and "Persistent=true" in timer and "Unit=snap-test.service" in timer

    def test_read_all_adds_the_capability(self, app_dir):
        result, out = self.render(app_dir, "--user", "jim", "--read-all")
        service = (out / "jabs-snapshot-agent.service").read_text()
        assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in service and "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in service

    def test_root_is_allowed_with_a_warning_and_needs_no_capability(self, app_dir):
        result, out = self.render(app_dir, "--user", "root", "--read-all")
        assert result.returncode == 0 and "runs as root" in result.stderr or "run as root" in result.stderr
        service = (out / "jabs-snapshot-agent.service").read_text()
        assert "User=root" in service and "CAP_DAC_READ_SEARCH" not in service

    @pytest.mark.parametrize("args", [["--name", "Bad_Name"], ["--user", "Bad User"]])
    def test_bad_values_are_rejected(self, app_dir, args):
        result, _ = self.render(app_dir, *args)
        assert result.returncode != 0 and "error:" in result.stderr

    def test_the_unit_files_pass_systemd_analyze(self, app_dir):
        if shutil.which("systemd-analyze") is None:
            pytest.skip("systemd-analyze is not installed")
        result, out = self.render(app_dir, "--user", "root")
        verify = subprocess.run(["systemd-analyze", "verify", str(out / "jabs-snapshot-agent.service"),
                                 str(out / "jabs-snapshot-agent.timer")], capture_output=True, text=True, check=False)
        assert "error" not in verify.stderr.lower().replace("no such file", ""), verify.stderr
