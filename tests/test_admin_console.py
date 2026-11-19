"""Console tests drive the real Textual app headlessly. Dialogs are filled in and buttons pressed like a user would."""

import asyncio
import sys

import pytest
from textual.widgets import Button, DataTable, OptionList, RichLog, Static, TabbedContent

from app import config, locking, ops, restic, runner, settings, state
from app.admin import runscreen, service
from app.admin.app import AdminApp
from app.admin.editor import EditorError
from app.admin.panes.diagnostics import DiagnosticsPane
from app.admin.panes.docs import DocsPane
from app.admin.panes.files import FilesPane
from app.admin.panes.jobs import JobsPane
from app.admin.panes.repository import RepositoryPane
from app.admin.panes.service import ServicePane

from .conftest import write_config, write_job, jid

JOB = 'job_name: "{name}"\npaths: [/data]\ntags: [{name}]\nschedules:\n  - cron: "0 3 * * *"\n    enabled: true\n'


def drive(scenario, size=(130, 45)):
    """Run `scenario(app, pilot)` against a fresh console and return its result."""
    async def main():
        app = AdminApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await scenario(app, pilot)

    return asyncio.run(main())


async def wait_for(pilot, predicate, what="condition", timeout=8.0):
    waited = 0.0
    while waited < timeout:
        await pilot.pause(0.05)
        if predicate():
            return
        waited += 0.05
    raise AssertionError(f"timed out waiting for {what}")


def dialog_title(app):
    labels = list(app.screen.query(".dialog-title"))
    return str(labels[0].render()) if labels else None


async def wait_dialog(pilot, app, title):
    await wait_for(pilot, lambda: dialog_title(app) == title, f"dialog '{title}'")


async def press(pilot, app, button_id):
    app.screen.query_one(f"#{button_id}", Button).press()
    await pilot.pause(0.1)


def pane_button(app, pane, button_id):
    return app.query_one(pane).query_one(f"#{button_id}", Button)


def table_rows(app, table_id):
    table = app.screen.query_one(f"#{table_id}", DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


def fill(app, **values):
    for key, value in values.items():
        app.screen.query_one(f"#f-{key}").value = value


def dialog_text(app):
    return " ".join(str(s.render()) for s in app.screen.query(Static))


async def open_tab(pilot, key):
    await pilot.press(key)
    await pilot.pause(0.3)


def run_output(app):
    return "\n".join(line.text for line in app.screen.query_one("#run-output", RichLog).lines)


def log_text(app, selector):
    return "\n".join(line.text for line in app.query_one(selector, RichLog).lines)


@pytest.fixture
def two_jobs(home):
    write_config(home, "repo_path: /repo\n")
    for name in ("alpha", "beta"):
        write_job(home, name, JOB.format(name=name))
    return home


@pytest.fixture
def child(monkeypatch):
    """Replace the child process with one that prints the arguments it was given."""
    seen = []

    def fake(args):
        seen.append(list(args))
        return [sys.executable, "-c", "import sys; print('ARGS', sys.argv[1:])", *args]

    monkeypatch.setattr(runscreen, "child_command", fake)
    return seen


class TestStartup:
    def test_opens_on_jobs_and_switches_tabs_with_number_keys(self, home):
        async def scenario(app, pilot):
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "jobs"
            for key, tab in (("2", "repository"), ("3", "files"), ("4", "diagnostics"), ("5", "service"),
                             ("6", "docs"), ("1", "jobs")):
                await pilot.press(key)
                await pilot.pause(0.1)
                assert tabs.active == tab

        drive(scenario)

    def test_starts_even_when_config_is_broken_and_says_so(self, home):
        write_config(home, "retention:\n  keep_dailyy: 7\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "needs attention" in str(app.query_one("#jobs-note", Static).render()))

        drive(scenario)


class TestJobs:
    def test_lists_jobs_with_schedule_next_run_and_last_result(self, two_jobs):
        state.record_run(jid("alpha"), {"success": True, "finished_at": 1_800_000_000})

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            rows = {r[0]: r for r in table_rows(app, "jobs-table")}
            assert rows["alpha"][1] == "0 3 * * *" and rows["alpha"][3].startswith("ok 2027-")
            assert rows["alpha"][2] != "-" and rows["alpha"][4] == "idle" and rows["beta"][3] == "-"

        drive(scenario)

    def test_a_running_job_is_marked(self, two_jobs):
        lock = locking.acquire_lock(runner.lock_path(jid("beta")))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            rows = {r[0]: r[4] for r in table_rows(app, "jobs-table")}
            assert rows["beta"].startswith("RUNNING (pid ") and rows["alpha"] == "idle"

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)

    def test_a_broken_job_file_is_reported_not_fatal(self, two_jobs):
        write_job(two_jobs, "broken", "paths: []\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "problem" in str(app.query_one("#jobs-note", Static).render()))
            assert len(table_rows(app, "jobs-table")) == 2

        drive(scenario)

    def test_dry_run_is_preselected_and_runs_the_job_with_the_flag(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "Dry run: alpha")
            assert app.screen.query_one("#f-dry_run").value is True
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled)
            assert "Finished: exit status 0 (success)" in str(app.screen.query_one("#run-status", Static).render())
            await press(pilot, app, "close")

        drive(scenario)
        (args,) = child
        assert args == ["run", str(settings.JOBS_DIR / "alpha.yaml"), "--dry-run"]

    def test_a_live_run_has_no_dry_run_flag_and_honours_the_options(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Back up: alpha")
            assert app.screen.query_one("#f-dry_run").value is False
            app.screen.query_one("#f-init").value = True
            app.screen.query_one("#f-prune").value = True
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled)

        drive(scenario)
        assert child == [["run", str(settings.JOBS_DIR / "alpha.yaml"), "--init", "--prune"]]

    def test_unticking_dry_run_in_the_form_makes_it_live(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "Dry run: alpha")
            app.screen.query_one("#f-dry_run").value = False
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert "--dry-run" not in child[0]

    def test_cancelling_the_form_runs_nothing(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Back up: alpha")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert dialog_title(app) is None

        drive(scenario)
        assert child == []

    def test_the_selected_job_is_the_one_that_runs(self, two_jobs, child):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            app.query_one("#jobs-table", DataTable).focus()
            await pilot.press("down")
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "Dry run: beta")

        drive(scenario)

    def test_a_job_that_is_already_running_is_refused(self, two_jobs, child):
        lock = locking.acquire_lock(runner.lock_path(jid("alpha")))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Error")
            assert "already running" in dialog_text(app)

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)
        assert child == []

    def test_no_job_selected_explains(self, home):
        write_config(home)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "No jobs yet" in str(app.query_one("#jobs-note", Static).render()))
            pane_button(app, JobsPane, "dry_run").press()
            await wait_dialog(pilot, app, "No job selected")

        drive(scenario)

    def test_a_failing_run_is_shown_as_failed(self, two_jobs, monkeypatch):
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Back up: alpha")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "FAILED" in str(app.screen.query_one("#run-status", Static).render()))
            assert "boom" in run_output(app)

        drive(scenario)

    def test_stop_interrupts_a_running_job(self, two_jobs, monkeypatch):
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Back up: alpha")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "started" in run_output(app))
            assert app.screen.query_one("#close", Button).disabled is True
            await press(pilot, app, "stop")
            await wait_for(pilot, lambda: "Stopped" in str(app.screen.query_one("#run-status", Static).render()))
            assert app.screen.query_one("#close", Button).disabled is False

        drive(scenario)

    def test_closing_the_console_during_a_run_leaves_nothing_behind(self, two_jobs, monkeypatch, tmp_path):
        marker = tmp_path / "pid"
        monkeypatch.setattr(runscreen, "child_command", lambda args: [
            sys.executable, "-c", f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"])

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "backup").press()
            await wait_dialog(pilot, app, "Back up: alpha")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: marker.exists() and marker.read_text())

        drive(scenario)
        import os
        import time
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail("the child process was left running")

    def test_new_job_creates_the_file_from_the_template_and_opens_the_editor(self, two_jobs, monkeypatch):
        template = settings.CONFIG_DIR / "templates"
        template.mkdir()
        (template / "job.yaml").write_text(
            'job_id: "00000000-0000-4000-8000-000000000000"\njob_name: "example"\npaths: ["/p"]\ntags: ["example"]\n')
        opened = []

        async def run_editor(self, path):
            opened.append(path)
            return 0

        monkeypatch.setattr(AdminApp, "run_editor", run_editor)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "new").press()
            await wait_dialog(pilot, app, "New job")
            fill(app, name="gamma")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: opened)
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 3)

        drive(scenario)
        text = (settings.JOBS_DIR / "gamma.yaml").read_text()
        assert 'job_name: "gamma"' in text and "00000000-0000" not in text and "job_id:" in text
        assert opened == [settings.JOBS_DIR / "gamma.yaml"]

    def test_add_missing_ids_gives_old_job_files_an_id(self, home):
        write_config(home, "repo_path: /repo\n")
        (settings.JOBS_DIR / "old.yaml").write_text("paths: [/d]\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "have problems" in str(app.query_one("#jobs-note", Static).render()))
            pane_button(app, JobsPane, "adopt").press()
            await wait_dialog(pilot, app, "Add missing ids")
            assert "old.yaml" in dialog_text(app)
            await press(pilot, app, "close")
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))

        drive(scenario)
        assert "job_id:" in (settings.JOBS_DIR / "old.yaml").read_text()

    @pytest.mark.parametrize("name,message", [("bad name", "letters"), ("alpha", "already exists")])
    def test_new_job_rejects_bad_or_duplicate_names(self, two_jobs, name, message):
        (settings.CONFIG_DIR / "templates").mkdir()
        (settings.CONFIG_DIR / "templates" / "job.yaml").write_text(
            'job_id: "00000000-0000-4000-8000-000000000000"\njob_name: "example"\npaths: ["/p"]\n')

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "new").press()
            await wait_dialog(pilot, app, "New job")
            fill(app, name=name)
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: message in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)

    def test_edit_job_validates_and_can_revert(self, two_jobs, monkeypatch):
        before = (settings.JOBS_DIR / "alpha.yaml").read_text()

        async def run_editor(self, path):
            path.write_text("paths: []\n")
            return 0

        monkeypatch.setattr(AdminApp, "run_editor", run_editor)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "edit").press()
            await wait_dialog(pilot, app, "jobs/alpha.yaml is not valid")
            await press(pilot, app, "choice-1")
            await wait_dialog(pilot, app, "Reverted")

        drive(scenario)
        assert (settings.JOBS_DIR / "alpha.yaml").read_text() == before


SNAPSHOTS = [
    {"short_id": "aaaa1111", "id": "a" * 64, "time": "2027-01-14T03:00:00.1Z", "hostname": "P3Tiny", "tags": ["alpha"], "paths": ["/data"]},
    {"short_id": "bbbb2222", "id": "b" * 64, "time": "2027-01-15T03:00:00.1Z", "hostname": "P3Tiny", "tags": ["beta"], "paths": ["/srv"]},
]


class TestRepository:
    @pytest.fixture
    def repo(self, two_jobs, monkeypatch, child):
        monkeypatch.setenv("RESTIC_PASSWORD", "pw")
        seen = []

        def snapshots(cfg, job_name=None):
            seen.append(job_name)
            return [s for s in SNAPSHOTS if job_name is None or any(job_name == jid(tag) for tag in s["tags"])]

        monkeypatch.setattr(ops, "snapshot_list", snapshots)
        return seen

    def test_lists_snapshots_newest_first(self, repo):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: len(table_rows(app, "snapshots")) == 2)
            rows = table_rows(app, "snapshots")
            assert [r[0] for r in rows] == ["bbbb2222", "aaaa1111"]
            assert rows[0][1] == "2027-01-15 03:00:00" and rows[0][3] == "beta"
            assert "2 snapshot(s)" in str(app.query_one("#repo-note", Static).render())

        drive(scenario)

    def test_filter_by_job(self, repo):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "job_filter").press()
            await wait_dialog(pilot, app, "Show snapshots of...")
            await pilot.press("down", "enter")            # alpha
            await wait_for(pilot, lambda: len(table_rows(app, "snapshots")) == 1)
            assert table_rows(app, "snapshots")[0][0] == "aaaa1111" and "for job 'alpha'" in str(
                app.query_one("#repo-note", Static).render())

        drive(scenario)
        assert repo[-1] == jid("alpha")

    def test_a_repository_problem_is_shown_in_the_tab_not_as_a_crash(self, two_jobs, monkeypatch):
        def broken(cfg, job_name=None):
            raise restic.ResticError("repository does not exist")

        monkeypatch.setattr(ops, "snapshot_list", broken)

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: "Cannot read the repository" in str(app.query_one("#repo-note", Static).render()))
            assert "does not exist" in str(app.query_one("#repo-note", Static).render())

        drive(scenario)

    def test_list_files_runs_ls_for_the_selected_snapshot(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "ls").press()
            await wait_dialog(pilot, app, "List files in bbbb2222")
            fill(app, path="/srv/docs")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["ls", "bbbb2222", "/srv/docs"]]

    def test_list_files_without_a_path(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "ls").press()
            await wait_dialog(pilot, app, "List files in bbbb2222")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["ls", "bbbb2222"]]

    def test_restore_runs_the_cli_with_target_and_path(self, repo, child, tmp_path):
        target = tmp_path / "out"

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "restore").press()
            await wait_dialog(pilot, app, "Restore bbbb2222")
            fill(app, target=str(target), path="/srv/a.txt")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["restore", "bbbb2222", str(target), "--path", "/srv/a.txt"]]

    def test_restore_needs_a_target(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "restore").press()
            await wait_dialog(pilot, app, "Restore bbbb2222")
            fill(app, target="")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "enter a directory" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)
        assert child == []

    def test_restoring_into_a_non_empty_directory_asks_first(self, repo, child, tmp_path):
        (tmp_path / "busy").mkdir()
        (tmp_path / "busy" / "x").write_text("1")

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "snapshots"))
            pane_button(app, RepositoryPane, "restore").press()
            await wait_dialog(pilot, app, "Restore bbbb2222")
            fill(app, target=str(tmp_path / "busy"))
            await press(pilot, app, "save")
            await wait_dialog(pilot, app, "Directory is not empty")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)

        drive(scenario)
        assert child == []

    def test_find_requires_a_query_and_passes_it_on(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            pane_button(app, RepositoryPane, "find").press()
            await wait_dialog(pilot, app, "Find a file in all snapshots")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "enter something" in str(app.screen.query_one("#error", Static).render()))
            fill(app, query="report")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["find", "report"]]

    @pytest.mark.parametrize("choice,flags", [("choice-0", []), ("choice-1", ["--subset", "5%"]), ("choice-2", ["--deep"])])
    def test_check_variants(self, repo, child, choice, flags):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            pane_button(app, RepositoryPane, "check").press()
            await wait_dialog(pilot, app, "Check the repository")
            await press(pilot, app, choice)
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["check", *flags]]

    def test_cancelling_the_check_runs_nothing(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            pane_button(app, RepositoryPane, "check").press()
            await wait_dialog(pilot, app, "Check the repository")
            await press(pilot, app, "choice-3")
            await pilot.pause(0.2)

        drive(scenario)
        assert child == []

    def test_init_asks_then_runs_init_repo(self, repo, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            pane_button(app, RepositoryPane, "init").press()
            await wait_dialog(pilot, app, "Create the repository")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["init-repo"]]

    def test_nothing_selected_explains(self, two_jobs, monkeypatch):
        monkeypatch.setattr(ops, "snapshot_list", lambda cfg, job_name=None: [])

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: "No snapshots" in str(app.query_one("#repo-note", Static).render()))
            pane_button(app, RepositoryPane, "restore").press()
            await wait_dialog(pilot, app, "No snapshot selected")

        drive(scenario)


class TestFiles:
    @pytest.fixture
    def editor(self, monkeypatch):
        class Fake:
            script = []
            launches = []
            error = None
            reinstalls = []

        async def run_editor(self, path):
            if Fake.error:
                raise Fake.error
            Fake.launches.append(path)
            if Fake.script:
                path.write_text(Fake.script.pop(0), encoding="utf-8")
            return 0

        async def reinstall_units(self):
            Fake.reinstalls.append(1)

        Fake.script, Fake.launches, Fake.error, Fake.reinstalls = [], [], None, []
        monkeypatch.setattr(AdminApp, "run_editor", run_editor)
        monkeypatch.setattr(AdminApp, "reinstall_units", reinstall_units)
        return Fake

    def test_lists_config_env_excludes_and_jobs_with_their_state(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: len(table_rows(app, "files-table")) == 5)
            rows = {r[0]: r[1] for r in table_rows(app, "files-table")}
            assert rows == {"config.yaml": "OK", ".env": "missing", "common_exclude.yaml": "missing",
                            "jobs/alpha.yaml": "OK", "jobs/beta.yaml": "OK"}

        drive(scenario)

    def test_an_env_readable_by_others_is_flagged(self, two_jobs):
        settings.ENV_PATH.write_text("RESTIC_PASSWORD=x\n")
        settings.ENV_PATH.chmod(0o644)

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: len(table_rows(app, "files-table")) == 5)
            assert {r[0]: r[1] for r in table_rows(app, "files-table")}[".env"] == "WARNING"

        drive(scenario)

    def test_an_invalid_edit_can_be_reverted_kept_or_fixed(self, two_jobs, editor):
        before = settings.CONFIG_PATH.read_text()
        editor.script = ["retention:\n  nope: 1\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            assert "retention.nope" in dialog_text(app)
            await press(pilot, app, "choice-1")
            await wait_dialog(pilot, app, "Reverted")

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == before

    def test_edit_again_then_valid_is_saved(self, two_jobs, editor):
        editor.script = ["retention:\n  nope: 1\n", "repo_path: /new\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            await press(pilot, app, "choice-0")
            await wait_for(pilot, lambda: len(editor.launches) == 2)
            await pilot.pause(0.3)

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == "repo_path: /new\n"

    def test_a_valid_edit_needs_no_restart_and_no_prompt(self, two_jobs, editor):
        editor.script = ["repo_path: /repo\nverify_after_backup: false\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.5)
            assert dialog_title(app) is None

        drive(scenario)
        assert not editor.reinstalls

    def test_changing_a_writable_directory_offers_to_reinstall_the_units(self, two_jobs, editor, tmp_path):
        local = tmp_path / "newrepo"
        local.mkdir()
        editor.script = [f"repo_path: {local}\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "The service's writable directories changed")
            assert f"+ {local}" in dialog_text(app)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: editor.reinstalls)

        drive(scenario)

    def test_a_new_env_is_created_private(self, two_jobs, editor):
        editor.script = ["RESTIC_PASSWORD=x\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one("#files-table", DataTable).focus()
            await pilot.press("down")
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.4)

        drive(scenario)
        assert (settings.ENV_PATH.stat().st_mode & 0o777) == 0o600

    def test_no_editor_available_is_reported(self, two_jobs, editor):
        editor.error = EditorError("no editor found; set the EDITOR environment variable")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "Error")
            assert "EDITOR" in dialog_text(app)

        drive(scenario)

    def test_validate_all_reports_every_file(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#validate", Button).press()
            await wait_dialog(pilot, app, "Validation")
            text = dialog_text(app)
            assert "config.yaml: OK" in text and "jobs/alpha.yaml: OK" in text and ".env: missing" in text

        drive(scenario)


class TestDiagnostics:
    def test_tick_asks_first_then_runs_it(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "tick").press()
            await wait_dialog(pilot, app, "Run the tick now")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert child == []
            pane_button(app, DiagnosticsPane, "tick").press()
            await wait_dialog(pilot, app, "Run the tick now")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["tick"]]

    def test_testmail_runs_the_cli_command(self, two_jobs, child):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "testmail").press()
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["testmail"]]

    @pytest.mark.parametrize("choice,flags", [("choice-0", []), ("choice-1", ["--print"])])
    def test_storage_can_send_or_only_print(self, two_jobs, child, choice, flags):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "storage").press()
            await wait_dialog(pilot, app, "Measure storage")
            await press(pilot, app, choice)
            await wait_for(pilot, lambda: "ARGS" in run_output(app))

        drive(scenario)
        assert child == [["report-storage", *flags]]

    def test_dashboard_link_in_standalone_mode_explains_how_to_enable_it(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "dashboard").press()
            await wait_dialog(pilot, app, "Dashboard link")
            assert "standalone" in dialog_text(app) and "JABS_DASHBOARD_URL" in dialog_text(app)

        drive(scenario)

    @pytest.mark.parametrize("accepted,expect", [(True, "was accepted"), (False, "NOT accepted")])
    def test_dashboard_link_sends_a_heartbeat(self, two_jobs, monkeypatch, accepted, expect):
        from app import dashboard

        monkeypatch.setenv("JABS_DASHBOARD_URL", "http://dash:5002")
        monkeypatch.setenv("JABS_AGENT_KEY", "k")
        calls = []
        monkeypatch.setattr(dashboard.Client, "send_heartbeat", lambda self: calls.append(1) or accepted)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "dashboard").press()
            await wait_dialog(pilot, app, "Dashboard link")
            assert expect in dialog_text(app) and "http://dash:5002" in dialog_text(app)

        drive(scenario)
        assert calls == [1]

    def test_state_view_shows_results_and_running_jobs(self, two_jobs):
        state.record_run(jid("alpha"), {"success": True, "finished_at": 1_800_000_000})
        lock = locking.acquire_lock(runner.lock_path(jid("beta")))

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "state").press()
            await wait_dialog(pilot, app, "Schedule state")
            text = dialog_text(app)
            assert '"alpha"' in text and '"running"' in text and '"beta"' in text

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)

    def test_paths_lists_the_writable_directories(self, two_jobs):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "paths").press()
            await wait_dialog(pilot, app, "Directories the service may write")
            assert str(settings.DATA_DIR) in dialog_text(app)

        drive(scenario)

    def test_reset_needs_the_word_and_removes_state_and_locks(self, two_jobs):
        state.record_run(jid("alpha"), {"success": True, "finished_at": 1})
        assert settings.STATE_PATH.exists()

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "reset").press()
            await wait_dialog(pilot, app, "Reset schedule state")
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "RESET"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "Reset")

        drive(scenario)
        assert not settings.STATE_PATH.exists()

    def test_cancelling_the_reset_changes_nothing(self, two_jobs):
        state.record_run(jid("alpha"), {"success": True, "finished_at": 1})

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            pane_button(app, DiagnosticsPane, "reset").press()
            await wait_dialog(pilot, app, "Reset schedule state")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)

        drive(scenario)
        assert settings.STATE_PATH.exists()


class TestService:
    @pytest.fixture
    def units(self, monkeypatch):
        calls = []
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: "jabs-snapshot-agent")
        monkeypatch.setattr(service, "unit_status", lambda unit: {
            "active": "inactive (dead)", "enabled": "enabled", "since": "today", "next": "in 3 min", "last": "2 min ago"})
        monkeypatch.setattr(service, "installed_options", lambda base: {"user": "jim", "read_all": True})

        async def run_systemctl(self, action, unit):
            calls.append((action, unit))
            return 0

        async def run_install(self, base, user, read_all):
            calls.append(("install", base, user, read_all))
            return 0

        monkeypatch.setattr(AdminApp, "run_systemctl", run_systemctl)
        monkeypatch.setattr(AdminApp, "run_install", run_install)
        return calls

    def test_lists_the_service_and_the_timer(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: len(table_rows(app, "units")) == 2)
            assert [r[0] for r in table_rows(app, "units")] == ["jabs-snapshot-agent.service", "jabs-snapshot-agent.timer"]
            assert table_rows(app, "units")[1][5] == "in 3 min"

        drive(scenario)

    def test_start_runs_a_tick_now_without_asking(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "start").press()
            await wait_for(pilot, lambda: units == [("start", "jabs-snapshot-agent.service")])

        drive(scenario)

    def test_disabling_the_timer_asks_first(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            app.query_one("#units", DataTable).focus()
            await pilot.press("down")
            pane_button(app, ServicePane, "disable").press()
            await wait_dialog(pilot, app, "Disable jabs-snapshot-agent.timer")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert units == []
            pane_button(app, ServicePane, "disable").press()
            await wait_dialog(pilot, app, "Disable jabs-snapshot-agent.timer")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: units == [("disable", "jabs-snapshot-agent.timer")])

        drive(scenario)

    def test_reinstall_keeps_the_installed_user_and_options(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "install").press()
            await wait_dialog(pilot, app, "Reinstall jabs-snapshot-agent")
            assert app.screen.query_one("#f-user").value == "jim" and app.screen.query_one("#f-read_all").value is True
            await press(pilot, app, "save")
            await wait_dialog(pilot, app, "Reinstall the units")

        drive(scenario)
        assert units == [("install", "jabs-snapshot-agent", "jim", True)]

    def test_reinstall_rejects_an_unusual_user_name(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            pane_button(app, ServicePane, "install").press()
            await wait_dialog(pilot, app, "Reinstall jabs-snapshot-agent")
            fill(app, user="Bad User; rm")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "unusual user" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)
        assert units == []

    def test_without_systemd_it_says_so(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: False)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "systemd is not available" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)

    def test_unknown_install_points_at_the_unit_option_and_install_script(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: None)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "--unit" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)


class TestDocs:
    @pytest.fixture
    def tree(self, home):
        (home / "README.md").write_text("# Install guide\n\nSee [the guide](docs/GUIDE.md).\n")
        (home / "docs").mkdir()
        (home / "docs" / "GUIDE.md").write_text("# Guide\n")
        (home / ".venv").mkdir()
        (home / ".venv" / "skip.md").write_text("no")
        return home

    def test_lists_markdown_files_and_shows_the_readme(self, tree):
        async def scenario(app, pilot):
            await open_tab(pilot, "6")
            pane = app.query_one(DocsPane)
            await wait_for(pilot, lambda: pane._current is not None)
            options = app.query_one("#docs-files", OptionList)
            assert [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)] == [
                "README.md", "docs/GUIDE.md"]
            await wait_for(pilot, lambda: "Install guide" in app.query_one("#docs-viewer").document.source)

        drive(scenario)

    def test_links_open_documents_and_outside_paths_do_not(self, tree):
        async def scenario(app, pilot):
            await open_tab(pilot, "6")
            pane = app.query_one(DocsPane)
            await wait_for(pilot, lambda: pane._current is not None)
            await pane._follow("https://example.com")
            await pane._follow("../x.md")
            assert pane._current.name == "README.md"
            await pane._follow("docs/GUIDE.md")
            assert pane._current.name == "GUIDE.md"
            pane.action_back()
            await wait_for(pilot, lambda: pane._current.name == "README.md")

        drive(scenario)


class TestRealRestic:
    """A dry run and a live run through the console, with a real restic repository and a real child process."""

    @pytest.fixture
    def real(self, home, tmp_path, monkeypatch):
        import shutil

        if shutil.which("restic") is None:
            pytest.skip("restic is not installed")
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.txt").write_text("hello\n")
        repo = tmp_path / "repo"
        write_config(home, f"repo_path: {repo}\nrestic_options: {{cache_dir: {tmp_path / 'cache'}}}\n")
        write_job(home, "docs", f'job_name: docs\npaths: ["{source}"]\ntags: [docs]\n')
        monkeypatch.setenv("JABS_HOME", str(home))
        monkeypatch.setenv("RESTIC_PASSWORD", "console-test")
        env = {"repo": repo}
        restic.init_repo(str(repo), "console-test", None, global_args=["--cache-dir", str(tmp_path / "cache")])
        return env

    def snapshots(self, real, tmp_path):
        return restic.list_snapshots(str(real["repo"]), tags=["docs"], password="console-test",
                                     global_args=["--cache-dir", str(tmp_path / "cache")])

    def test_dry_run_saves_nothing_and_a_live_run_saves_a_snapshot(self, real, tmp_path):
        async def run_job(app, pilot, dry):
            pane_button(app, JobsPane, "dry_run" if dry else "backup").press()
            await wait_dialog(pilot, app, "Dry run: docs" if dry else "Back up: docs")
            if dry:
                assert app.screen.query_one("#f-dry_run").value is True
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: not app.screen.query_one("#close", Button).disabled, timeout=60)
            status = str(app.screen.query_one("#run-status", Static).render())
            output = run_output(app)
            await press(pilot, app, "close")
            await pilot.pause(0.3)
            return status, output

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            status, output = await run_job(app, pilot, dry=True)
            assert "exit status 0 (success)" in status and "Starting backup: docs" in output
            assert self.snapshots(real, tmp_path) == []
            assert table_rows(app, "jobs-table")[0][3].startswith("ok ")
            status, output = await run_job(app, pilot, dry=False)
            assert "exit status 0 (success)" in status and "Backup complete" in output
            assert len(self.snapshots(real, tmp_path)) == 1

        drive(scenario)


class TestDeleteJob:
    def test_needs_the_job_name_typed_then_removes_the_file_and_its_state(self, two_jobs):
        state.record_run(jid("alpha"), {"success": True, "finished_at": 1})
        state.record_run(jid("beta"), {"success": True, "finished_at": 1})

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "beta"
            await pilot.pause(0.1)
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word").value = "alpha"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: [r[0] for r in table_rows(app, "jobs-table")] == ["beta"])

        drive(scenario)
        assert not (settings.JOBS_DIR / "alpha.yaml").exists() and (settings.JOBS_DIR / "beta.yaml").exists()
        assert list(state.read()["jobs"]) == [jid("beta")]

    def test_the_selected_job_is_the_one_deleted(self, two_jobs):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "jobs-table")) == 2)
            app.query_one("#jobs-table", DataTable).focus()
            await pilot.press("down")
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job beta")

        drive(scenario)
        assert (settings.JOBS_DIR / "beta.yaml").exists()

    def test_cancelling_keeps_the_job(self, two_jobs):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert dialog_title(app) is None

        drive(scenario)
        assert (settings.JOBS_DIR / "alpha.yaml").exists()

    def test_a_running_job_cannot_be_deleted(self, two_jobs):
        lock = locking.acquire_lock(runner.lock_path(jid("alpha")))

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Error")
            assert "is running" in dialog_text(app)

        try:
            drive(scenario)
        finally:
            locking.release_lock(lock)
        assert (settings.JOBS_DIR / "alpha.yaml").exists()

    def test_no_job_selected_explains(self, home):
        write_config(home)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "No jobs yet" in str(app.query_one("#jobs-note", Static).render()))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "No job selected")

        drive(scenario)

    def test_removing_a_jobs_own_local_repository_offers_to_reinstall_the_units(self, home, monkeypatch, tmp_path):
        local = tmp_path / "own-repo"
        local.mkdir()
        write_config(home, "repo_path: /repo\n")
        write_job(home, "alpha", JOB.format(name="alpha") + f"repo_path: {local}\n")
        reinstalls = []

        async def reinstall_units(self):
            reinstalls.append(1)

        monkeypatch.setattr(AdminApp, "reinstall_units", reinstall_units)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job alpha")
            app.screen.query_one("#word").value = "alpha"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "The service's writable directories changed")
            assert f"- {local}" in dialog_text(app)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: reinstalls)

        drive(scenario)

    def test_a_job_whose_name_differs_from_its_file_deletes_the_right_file(self, home):
        write_config(home)
        write_job(home, "file-name", 'job_name: "display-name"\npaths: [/d]\n')

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "jobs-table"))
            pane_button(app, JobsPane, "delete").press()
            await wait_dialog(pilot, app, "Delete job display-name")
            app.screen.query_one("#word").value = "display-name"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: not (settings.JOBS_DIR / "file-name.yaml").exists())

        drive(scenario)
