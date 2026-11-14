"""Jobs tab: what is scheduled, and running a configured job now, live or as a dry run."""

import asyncio
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import RowDoesNotExist

from ... import config, ops, settings
from .. import editing
from ..app import Pane
from ..checks import new_job_file
from ..logs import audit

LIVE_NOTE = ("Backs up the job's paths now, exactly like a scheduled run: reports to the dashboard, emails on failure "
             "and pings Uptime Kuma if configured.")
DRY_NOTE = ("restic --dry-run: nothing is saved, so no snapshot is created. The run is still reported to the "
            "dashboard, marked [DRY RUN].")


class JobsPane(Pane):
    TAB_ID = "jobs"
    TAB_TITLE = "1 Jobs"
    ACTIONS = {"jobs.list": "reload", "jobs.run": "do_run", "jobs.edit": "do_edit", "jobs.new": "do_new"}
    BINDINGS = [
        Binding("d", "dry_run", "Dry run"), Binding("b", "backup", "Back up now"), Binding("e", "edit", "Edit"),
        Binding("n", "new", "New job"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._jobs: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="jobs-note", markup=False, classes="note")
        yield DataTable(id="jobs-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Dry run", id="dry_run", variant="primary")
            yield Button("Back up now", id="backup", variant="success")
            yield Button("Edit job file", id="edit")
            yield Button("New job", id="new")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Job", "Schedule", "Next run", "Last run", "Now")
        self.set_interval(10, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.admin.active_pane() is self:
            self.admin.spawn(self.reload)

    def selected(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#jobs-note", Static)
        table = self.query_one(DataTable)
        keep = self.selected()
        try:
            cfg = await asyncio.to_thread(config.load_config)
            rows, errors = await asyncio.to_thread(ops.jobs_overview, cfg)
        except config.ConfigError as exc:
            table.clear()
            self._jobs = {}
            note.update(f"config.yaml needs attention (Files tab):\n{exc}")
            return
        self._jobs = {row["job"].name: row["job"] for row in rows}
        table.clear()
        for row in rows:
            job = row["job"]
            nxt = row["next"]
            running = row["running"]
            table.add_row(
                job.name, ", ".join(job.enabled_crons()) or "none enabled",
                nxt.strftime("%Y-%m-%d %H:%M") if nxt else "-", ops.format_last(row["last"]),
                f"RUNNING (pid {running.get('pid', '?')})" if running is not None else "idle", key=job.name)
        if keep is not None:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:
                pass
        if errors:
            note.update(f"{len(errors)} job file(s) have problems and are not listed; see the Files tab.\n" + errors[0].splitlines()[0])
        elif rows:
            note.update(f"{len(rows)} job(s). Dry run (d) tests a job without saving anything; Back up now (b) runs it for real.")
        else:
            note.update(f"No jobs yet. Press n to create one from the template in {settings.JOBS_DIR}.")

    # ---------------------------------------------------------------- actions

    async def do_run(self, dry_run: bool = True) -> None:
        name = self.selected()
        job = self._jobs.get(name) if name else None
        if job is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        lock = await asyncio.to_thread(ops.job_lock_info, job.name)
        if lock is not None:
            await self.admin.error(f"'{job.name}' is already running (pid {lock.get('pid', '?')}). Wait for it to finish.")
            return
        fields = [
            {"key": "dry_run", "label": "Dry run (nothing is saved)", "kind": "bool", "value": dry_run,
             "help": DRY_NOTE if dry_run else LIVE_NOTE},
            {"key": "init", "label": "Create the repository first if it does not exist (--init)", "kind": "bool", "value": False},
            {"key": "prune", "label": "Forget and prune old snapshots afterwards (--prune)", "kind": "bool", "value": False},
        ]
        values = await self.admin.form(f"{'Dry run' if dry_run else 'Back up'}: {job.name}", fields, lambda v: v,
                                       "Run")
        if values is None:
            return
        args = ["run", job.path]
        args += [flag for flag, key in (("--dry-run", "dry_run"), ("--init", "init"), ("--prune", "prune")) if values[key]]
        mode = "dry run" if values["dry_run"] else "live backup"
        audit("job run started", job=job.name, mode=mode, init=values["init"], prune=values["prune"])
        code = await self.admin.run_command(f"{'Dry run' if values['dry_run'] else 'Backup'}: {job.name}", args,
                                            DRY_NOTE if values["dry_run"] else LIVE_NOTE)
        audit("job run finished", job=job.name, mode=mode, exit=code)
        await self.reload()

    async def do_edit(self) -> None:
        name = self.selected()
        entry = editing.entry_for_path(self._jobs[name].path) if name in self._jobs else None
        if entry is None:
            await self.admin.show("No job selected", "Select a job in the table first.")
            return
        await editing.edit_entry(self.admin, entry)
        await self.reload()

    async def do_new(self) -> None:
        fields = [{"key": "name", "label": "Job name", "kind": "text",
                   "help": "Letters, digits, . _ and -. It becomes config/jobs/<name>.yaml, created from the template "
                           "with its schedule disabled."}]

        def submit(values: dict):
            return new_job_file(values["name"])

        path = await self.admin.form("New job", fields, submit, "Create")
        if path is None:
            return
        audit("job file created", file=path.name)
        entry = editing.entry_for_path(path)
        await self.reload()
        if entry is not None:
            await editing.edit_entry(self.admin, entry)
            await self.reload()

    # ---------------------------------------------------------------- bindings and buttons

    def action_dry_run(self) -> None:
        self.admin.spawn(self.do_run, True)

    def action_backup(self) -> None:
        self.admin.spawn(self.do_run, False)

    def action_edit(self) -> None:
        self.admin.spawn(self.do_edit)

    def action_new(self) -> None:
        self.admin.spawn(self.do_new)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_dry_run()
