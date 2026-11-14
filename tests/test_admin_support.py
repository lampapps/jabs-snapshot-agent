import argparse
import logging
import os
import stat
import subprocess
import sys

import pytest

from app import __main__ as cli
from app import admin, config, ops, settings
from app.admin import checks, editor, logs, runscreen, service, util
from app.admin.panes import PANES

from .conftest import write_config, write_job


def leaf_commands(parser, prefix=""):
    """'agent add' style paths of every runnable subcommand in the argparse tree."""
    leaves = []
    for action in parser._actions:  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                below = leaf_commands(sub, path)
                leaves += below or [path]
    return leaves


class TestParity:
    """The console must be able to do everything the command line can (README: 'Commands')."""

    def test_every_subcommand_has_a_console_action(self):
        missing = [c for c in leaf_commands(cli.build_parser()) if c not in admin.COVERAGE and c not in admin.NOT_COVERED]
        assert not missing, f"add these to admin.COVERAGE (and a tab action): {missing}"

    def test_no_stale_entries(self):
        leaves = set(leaf_commands(cli.build_parser()))
        stale = [c for c in list(admin.COVERAGE) + list(admin.NOT_COVERED) if c not in leaves]
        assert not stale, f"not subcommands any more: {stale}"

    def test_every_action_id_points_at_a_real_method_on_a_tab(self):
        registered = {}
        for pane in PANES:
            for action_id, method in pane.ACTIONS.items():
                assert callable(getattr(pane, method, None)), f"{pane.__name__}.{method} does not exist"
                registered[action_id] = pane
        for command, action_id in admin.COVERAGE.items():
            assert action_id in registered, f"'{command}' maps to unknown action '{action_id}'"

    def test_tab_ids_and_numbers_are_unique(self):
        assert len({p.TAB_ID for p in PANES}) == len(PANES)
        assert [p.TAB_TITLE.split()[0] for p in PANES] == [str(i) for i in range(1, len(PANES) + 1)]


class TestRunGuards:
    def test_root_is_refused_when_the_install_belongs_to_someone_else(self, home, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        message = admin.check_user()
        if os.stat(home).st_uid == 0:
            assert message is None
        else:
            assert "not as root" in message and "sudo -u" in message

    def test_a_user_who_cannot_write_the_data_directory_is_told_to_switch(self, home):
        if os.geteuid() == 0:
            pytest.skip("root can always write")
        settings.DATA_DIR.mkdir()
        settings.DATA_DIR.chmod(0o555)
        try:
            assert "not writable" in admin.check_user()
        finally:
            settings.DATA_DIR.chmod(0o755)

    def test_a_normal_user_with_a_writable_install_is_fine(self, home):
        assert admin.check_user() is None

    def test_missing_textual_is_explained(self, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "textual", None)
        assert admin.run() == 1
        assert "textual" in capsys.readouterr().err

    def test_a_problem_user_stops_the_console_before_it_starts(self, home, monkeypatch, capsys):
        monkeypatch.setattr(admin, "check_user", lambda: "run it as someone else")
        assert admin.run() == 1
        assert "someone else" in capsys.readouterr().err

    def test_the_admin_command_starts_even_with_a_broken_config(self, home, monkeypatch):
        write_config(home, "retention:\n  nope: 1\n")
        seen = []
        monkeypatch.setattr(admin, "run", lambda unit=None: seen.append(unit) or 0)
        monkeypatch.setattr(cli.settings, "load_env", lambda: None)
        assert cli.main(["admin", "--unit", "jabs-x"]) == 0
        assert seen == ["jabs-x"]


class TestEditor:
    def test_visual_beats_editor_and_arguments_are_kept(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "nano -w")
        monkeypatch.setenv("EDITOR", "vim")
        monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert editor.find_editor() == ["nano", "-w"]

    def test_falls_back_to_the_system_default_then_common_editors(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "not-installed")
        monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/sensible-editor" if name == "sensible-editor" else None)
        assert editor.find_editor() == ["/usr/bin/sensible-editor"]
        monkeypatch.setattr(editor.shutil, "which", lambda name: f"/bin/{name}" if name == "vi" else None)
        assert editor.find_editor() == ["/bin/vi"]

    def test_none_found(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(editor.shutil, "which", lambda name: None)
        assert editor.find_editor() == []
        with pytest.raises(editor.EditorError, match="no editor"):
            editor.run_editor(settings.CONFIG_PATH)

    def test_runs_the_editor_on_the_file(self, tmp_path, monkeypatch):
        script = tmp_path / "ed.sh"
        script.write_text('#!/bin/sh\necho "edited" >> "$1"\n')
        script.chmod(0o755)
        monkeypatch.setenv("VISUAL", str(script))
        target = tmp_path / "f.txt"
        target.write_text("start\n")
        assert editor.run_editor(target) == 0
        assert target.read_text() == "start\nedited\n"


class TestChecks:
    def entry(self, kind):
        return next(e for e in checks.managed_files() if e["kind"] == kind)

    def test_env_permissions_are_checked(self, home):
        settings.ENV_PATH.write_text("RESTIC_PASSWORD=x\n")
        for mode, state in ((0o644, "warn"), (0o640, "warn"), (0o600, "ok")):
            settings.ENV_PATH.chmod(mode)
            assert checks.check_file(self.entry("env"))["state"] == state

    def test_config_states(self, home):
        write_config(home)
        assert checks.check_file(self.entry("config"))["state"] == "ok"
        write_config(home, "retention:\n  nope: 1\n")
        result = checks.check_file(self.entry("config"))
        assert result["state"] == "error" and "retention.nope" in result["message"]
        settings.CONFIG_PATH.unlink()
        assert checks.check_file(self.entry("config"))["state"] == "missing"

    def test_job_files_are_checked_against_the_global_config(self, home):
        write_config(home)
        write_job(home, "a")
        assert checks.check_file(self.entry("job"))["state"] == "ok"
        write_job(home, "a", "paths: []\n")
        assert checks.check_file(self.entry("job"))["state"] == "error"
        write_config(home, "retention:\n  nope: 1\n")
        assert checks.check_file(self.entry("job"))["state"] == "error"

    def test_the_exclude_list_must_be_a_list_of_patterns(self, home):
        assert checks.check_file(self.entry("exclude"))["state"] == "missing"
        settings.COMMON_EXCLUDE_PATH.write_text("- .cache/\n- '*.tmp'\n")
        assert checks.check_file(self.entry("exclude"))["state"] == "ok"
        settings.COMMON_EXCLUDE_PATH.write_text("a: 1\n")
        assert checks.check_file(self.entry("exclude"))["state"] == "error"
        settings.COMMON_EXCLUDE_PATH.write_text("- [unclosed\n")
        assert checks.check_file(self.entry("exclude"))["state"] == "error"

    def test_every_job_file_is_listed(self, home):
        write_config(home)
        write_job(home, "a")
        write_job(home, "b")
        assert [e["label"] for e in checks.managed_files()][3:] == ["jobs/a.yaml", "jobs/b.yaml"]

    def test_new_job_files_come_from_the_template_with_the_name_filled_in(self, home):
        (settings.CONFIG_DIR / "templates").mkdir()
        (settings.CONFIG_DIR / "templates" / "job.yaml").write_text('job_name: "example"\ntags:\n  - "example"\npaths: ["/p"]\n')
        path = checks.new_job_file("photos")
        assert path == settings.JOBS_DIR / "photos.yaml"
        assert path.read_text() == 'job_name: "photos"\ntags:\n  - "photos"\npaths: ["/p"]\n'
        with pytest.raises(ValueError, match="already exists"):
            checks.new_job_file("photos")

    @pytest.mark.parametrize("name", ["", "bad name", "../x", "-lead", "x" * 70])
    def test_new_job_names_are_validated(self, home, name):
        with pytest.raises(ValueError):
            checks.new_job_file(name)

    def test_a_missing_template_is_reported(self, home):
        with pytest.raises(ValueError, match="template"):
            checks.new_job_file("a")

    def test_revert_restores_content_and_removes_files_that_did_not_exist(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"old")
        before = checks.snapshot(path)
        path.write_bytes(b"new")
        checks.revert(path, before)
        assert path.read_bytes() == b"old"
        checks.revert(path, None)
        assert not path.exists()

    def test_revert_keeps_the_files_mode(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"old")
        path.chmod(0o600)
        checks.revert(path, b"older")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_only_config_has_a_starter_text(self, home):
        (settings.CONFIG_PATH.parent / "config.example.yaml").write_text("x: 1\n")
        assert checks.starter_text(self.entry("config")) == "x: 1\n"
        assert checks.starter_text(self.entry("env")) is None


class FakeSystemctl:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        for needle, text in self.outputs.items():
            if needle in " ".join(args):
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestService:
    def test_override_wins_and_loses_the_suffix(self):
        assert service.detect_base("jabs-x.service") == "jabs-x"

    def test_detects_the_unit_whose_working_directory_is_this_install(self, monkeypatch):
        fake = FakeSystemctl({
            "list-unit-files": "jabs-snapshot-agent.service static\njabs-snapshot-agent-nas.service static\n",
            "show jabs-snapshot-agent.service": "WorkingDirectory=/elsewhere\n",
            "show jabs-snapshot-agent-nas.service": f"WorkingDirectory={service.CODE_DIR}\n",
        })
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "_run", fake)
        assert service.detect_base() == "jabs-snapshot-agent-nas"

    def test_a_single_candidate_is_used_and_several_are_not_guessed(self, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "_run", FakeSystemctl({"list-unit-files": "jabs-snapshot-agent.service static\n"}))
        assert service.detect_base() == "jabs-snapshot-agent"
        monkeypatch.setattr(service, "_run", FakeSystemctl(
            {"list-unit-files": "jabs-snapshot-agent.service static\njabs-snapshot-agent-b.service static\n"}))
        assert service.detect_base() is None

    def test_not_on_systemd(self, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: False)
        assert service.detect_base() is None

    def test_unit_status_text(self, monkeypatch):
        monkeypatch.setattr(service, "_run", FakeSystemctl({"show": (
            "ActiveState=inactive\nSubState=dead\nUnitFileState=static\nResult=exit-code\nLastTriggerUSec=Fri\n")}))
        status = service.unit_status("x.service")
        assert status["active"] == "inactive (dead), last result exit-code" and status["last"] == "Fri"

    def test_unknown_unit(self, monkeypatch):
        monkeypatch.setattr(service, "_run", FakeSystemctl({}))
        assert service.unit_status("x.service")["active"] == "unknown"

    def test_commands_use_sudo_for_normal_users_only(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert service.control_command("restart", "u.timer") == ["sudo", "systemctl", "restart", "u.timer"]
        assert service.control_command("disable", "u.timer") == ["sudo", "systemctl", "disable", "--now", "u.timer"]
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert service.control_command("enable", "u.timer") == ["systemctl", "enable", "--now", "u.timer"]

    def test_only_known_actions_are_allowed(self):
        with pytest.raises(ValueError):
            service.control_command("mask", "u.service")

    def test_installed_options_come_from_the_unit_file(self, tmp_path, monkeypatch):
        (tmp_path / "jabs-x.service").write_text("[Service]\nUser=jim\nAmbientCapabilities=CAP_DAC_READ_SEARCH\n")
        monkeypatch.setattr(service, "UNIT_DIR", tmp_path)
        assert service.installed_options("jabs-x") == {"user": "jim", "read_all": True}
        (tmp_path / "jabs-x.service").write_text("[Service]\nUser=root\n")
        assert service.installed_options("jabs-x") == {"user": "root", "read_all": False}
        assert service.installed_options("missing") == {"user": "", "read_all": False}

    def test_install_command_repeats_the_choices(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        command = service.install_command("jabs-x", "jim", True)
        assert command[0] == "sudo" and command[1].endswith("deploy/install.sh")
        assert command[2:] == ["--name", "jabs-x", "--user", "jim", "--read-all"]
        assert "--read-all" not in service.install_command("jabs-x", "jim", False)

    def test_journal_command_follows_the_unit(self):
        command = service.journal_command("u.service")
        assert command[:3] == ["journalctl", "-u", "u.service"] and "-f" in command


class TestSupport:
    def test_ago(self):
        assert util.ago(None) == "-"
        assert util.ago(990, now=1000) == "10 s ago"
        assert util.ago(1000 - 600, now=1000) == "10 min ago"
        assert util.ago(1000 - 7200, now=1000) == "2 h ago"
        assert util.ago(1000 - 5 * 86400, now=1000) == "5 d ago"

    def test_one_line(self):
        assert util.one_line(None) == "-" and util.one_line("disk\n  full") == "disk full"
        assert util.one_line("x" * 100, limit=10).endswith("\u2026")

    def test_format_bytes(self):
        assert util.format_bytes(512) == "512 B" and util.format_bytes(1536) == "1.5 KiB"

    def test_child_commands_use_this_interpreter(self):
        assert runscreen.child_command(["run", "a"]) == [sys.executable, "-m", "app", "run", "a"]

    def test_capture_keeps_records_off_the_terminal(self, capsys):
        root = logging.getLogger()
        before = list(root.handlers)
        root.addHandler(logging.StreamHandler(sys.stderr))
        try:
            capture = logs.install()
            logging.getLogger("x").warning("careful")
            assert capture.drain() == ["WARNING x: careful"] and capture.drain() == []
            assert "careful" not in capsys.readouterr().err
        finally:
            root.handlers[:] = before

    def test_audit_lines_name_the_user_and_the_details(self, caplog):
        with caplog.at_level(logging.INFO, logger=logs.AUDIT_LOGGER):
            logs.audit("job run started", job="alpha")
        assert "job run started by" in caplog.text and "job=alpha" in caplog.text


class TestOps:
    def test_jobs_overview_reports_next_last_and_running(self, home):
        from app import locking, runner, state

        write_config(home)
        write_job(home, "a", 'paths: [/d]\nschedules:\n  - cron: "0 3 * * *"\n    enabled: true\n')
        write_job(home, "b", "paths: [/d]\n")
        state.record_run("a", {"success": False, "finished_at": 1_800_000_000})
        lock = locking.acquire_lock(runner.lock_path("b"))
        try:
            rows, errors = ops.jobs_overview(config.load_config())
        finally:
            locking.release_lock(lock)
        by_name = {r["job"].name: r for r in rows}
        assert errors == [] and by_name["a"]["next"] is not None and by_name["a"]["running"] is None
        assert ops.format_last(by_name["a"]["last"]).startswith("FAILED 2027-")
        assert by_name["b"]["next"] is None and by_name["b"]["running"]["pid"] == os.getpid()

    def test_a_stale_lock_file_is_not_running(self, home):
        from app import runner

        settings.LOCK_DIR.mkdir(parents=True)
        open(runner.lock_path("a"), "w", encoding="utf-8").write("{}")
        assert ops.job_lock_info("a") is None
        assert ops.job_lock_info("never-ran") is None

    def test_reset_removes_locks_and_state_only(self, home):
        from app import state

        state.record_run("a", {"success": True, "finished_at": 1})
        settings.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        (settings.LOCK_DIR / "a.lock").write_text("{}")
        (home / "keep.txt").write_text("x")
        assert ops.reset_state() >= 2
        assert not settings.STATE_PATH.exists() and not (settings.LOCK_DIR / "a.lock").exists()
        assert (home / "keep.txt").exists()

    def test_writable_paths_include_data_local_repo_and_cache(self, home, tmp_path):
        write_config(home, f"repo_path: {tmp_path / 'repo'}\nrestic_options: {{cache_dir: {tmp_path / 'cache'}}}\n")
        paths = ops.writable_paths(config.load_config())
        assert str(settings.DATA_DIR) in paths and str(tmp_path / "repo") in paths and str(tmp_path / "cache") in paths

    def test_remote_repositories_are_not_writable_paths(self, home):
        write_config(home, "repo_path: sftp:host:/repo\n")
        assert not any(p.startswith("sftp:") for p in ops.writable_paths(config.load_config()))

    def test_delete_job_removes_the_file_and_only_that_jobs_state(self, home):
        from app import state

        write_config(home)
        path = write_job(home, "a")
        write_job(home, "b")
        state.record_run("a", {"success": True, "finished_at": 1})
        state.record_run("b", {"success": True, "finished_at": 1})
        ops.delete_job(config.find_job("a", config.load_config()))
        assert not path.exists() and list(state.read()["jobs"]) == ["b"]

    def test_delete_job_refuses_files_outside_the_jobs_directory(self, home, tmp_path):
        write_config(home)
        outside = tmp_path / "elsewhere.yaml"
        outside.write_text("paths: [/d]\n")
        job = config.load_job(outside, config.load_config())
        with pytest.raises(config.ConfigError, match="delete it by hand"):
            ops.delete_job(job)
        assert outside.exists()

    def test_delete_job_refuses_a_running_job(self, home):
        from app import locking, runner

        write_config(home)
        path = write_job(home, "a")
        lock = locking.acquire_lock(runner.lock_path("a"))
        try:
            with pytest.raises(config.ConfigError, match="is running"):
                ops.delete_job(config.find_job("a", config.load_config()))
        finally:
            locking.release_lock(lock)
        assert path.exists()

    def test_snapshot_fields(self):
        snap = {"short_id": "abcd1234", "time": "2027-01-15T03:00:00.1Z", "hostname": "h", "tags": ["t"], "paths": ["/a", "/b"]}
        assert ops.snapshot_fields(snap) == ("abcd1234", "2027-01-15 03:00:00", "h", "t", "/a /b")
        assert ops.snapshot_fields({"id": "f" * 64}) == ("ffffffff", "", "", "-", "")

    def test_check_labels(self):
        assert ops.check_label() == "quick check (metadata only)"
        assert ops.check_label(deep=True) == "full data check"
        assert ops.check_label(subset="5%") == "partial data check (subset=5%)"

    def test_credentials_and_repo_are_required(self, home):
        write_config(home, "")
        with pytest.raises(config.ConfigError, match="RESTIC_PASSWORD"):
            ops.credentials()
        with pytest.raises(config.ConfigError, match="repo_path"):
            ops.repo_path(config.load_config())
