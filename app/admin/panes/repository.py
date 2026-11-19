"""Repository tab: snapshots, browsing, finding and restoring files, and checking the repository."""

import asyncio
from pathlib import Path
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ... import config, ops
from ...restic import ResticError
from ..app import Pane
from ..logs import audit


class RepositoryPane(Pane):
    TAB_ID = "repository"
    TAB_TITLE = "2 Repository"
    ACTIONS = {
        "repository.snapshots": "reload", "repository.ls": "do_ls", "repository.restore": "do_restore",
        "repository.find": "do_find", "repository.check": "do_check", "repository.init": "do_init",
    }
    BINDINGS = [
        Binding("l", "ls", "List files"), Binding("o", "restore", "Restore"), Binding("f", "find", "Find a file"),
        Binding("c", "check", "Check"), Binding("i", "init", "Init"), Binding("j", "job_filter", "Filter by job"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._job: Optional[str] = None       # a job's id
        self._job_name: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Static("", id="repo-note", markup=False, classes="note")
        yield DataTable(id="snapshots", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("List files", id="ls", variant="primary")
            yield Button("Restore", id="restore")
            yield Button("Find a file", id="find")
            yield Button("Filter by job", id="job_filter")
            yield Button("Check", id="check")
            yield Button("Init", id="init")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Snapshot", "Time", "Host", "Tags", "Paths")

    def selected(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#repo-note", Static)
        table = self.query_one(DataTable)
        note.update("Loading snapshots...")
        try:
            cfg = await asyncio.to_thread(config.load_config)
            snapshots = await asyncio.to_thread(ops.snapshot_list, cfg, self._job)
        except (config.ConfigError, ResticError) as exc:
            table.clear()
            note.update(f"Cannot read the repository:\n{exc}")
            return
        table.clear()
        for snap in sorted(snapshots, key=lambda s: s.get("time", ""), reverse=True):
            short, stamp, host, tags, paths = ops.snapshot_fields(snap)
            table.add_row(short, stamp, host, tags, paths, key=short)
        scope = f" for job '{self._job_name}'" if self._job else ""
        note.update(f"{len(snapshots)} snapshot(s){scope} in {cfg.repo_path}" if snapshots
                    else f"No snapshots{scope} in {cfg.repo_path}. If the repository does not exist yet, press i.")

    # ---------------------------------------------------------------- actions

    async def _snapshot(self) -> Optional[str]:
        snapshot = self.selected()
        if snapshot is None:
            await self.admin.show("No snapshot selected", "Select a snapshot in the table first.")
        return snapshot

    async def do_ls(self) -> None:
        snapshot = await self._snapshot()
        if snapshot is None:
            return
        fields = [{"key": "path", "label": "Only below this path (optional)", "kind": "text",
                   "help": "Empty lists every file in the snapshot, which can be very long."}]
        values = await self.admin.form(f"List files in {snapshot}", fields, lambda v: v, "List")
        if values is None:
            return
        await self.admin.run_command(f"Files in {snapshot}", ["ls", snapshot, *([values["path"]] if values["path"] else [])])

    async def do_restore(self) -> None:
        snapshot = await self._snapshot()
        if snapshot is None:
            return
        fields = [
            {"key": "target", "label": "Restore into this directory", "kind": "text",
             "value": str(Path.home() / f"restore-{snapshot}"), "help": "Created if missing. Files with the same names are overwritten."},
            {"key": "path", "label": "Only this path from the snapshot (optional)", "kind": "text",
             "help": "Empty restores the whole snapshot."},
        ]

        def submit(values: dict) -> dict:
            if not values["target"]:
                raise ValueError("enter a directory to restore into")
            return values

        values = await self.admin.form(f"Restore {snapshot}", fields, submit, "Restore")
        if values is None:
            return
        target = Path(values["target"]).expanduser()
        if target.exists() and any(target.iterdir()):
            ok, _ = await self.admin.confirm(
                "Directory is not empty", f"{target} already has files. Restoring writes over files with the same names.",
                confirm="Restore anyway", danger=True)
            if not ok:
                return
        args = ["restore", snapshot, str(target), *(["--path", values["path"]] if values["path"] else [])]
        audit("restore started", snapshot=snapshot, target=target, path=values["path"] or "-")
        code = await self.admin.run_command(f"Restore {snapshot}", args)
        audit("restore finished", snapshot=snapshot, exit=code)

    async def do_find(self) -> None:
        fields = [{"key": "query", "label": "File name or part of a path", "kind": "text",
                   "help": "Case-insensitive; * and ? work as wildcards."}]

        def submit(values: dict) -> dict:
            if not values["query"]:
                raise ValueError("enter something to search for")
            return values

        values = await self.admin.form("Find a file in all snapshots", fields, submit, "Search")
        if values is not None:
            await self.admin.run_command(f"Find '{values['query']}'", ["find", values["query"]])

    async def do_check(self) -> None:
        choice = await self.admin.choose(
            "Check the repository", "A quick check reads only metadata. Reading data takes much longer and downloads "
            "from remote repositories.",
            [("Quick (metadata)", []), ("Sample 5% of the data", ["--subset", "5%"]),
             ("Read all data (deep)", ["--deep"]), ("Cancel", None)])
        if choice is None:
            return
        await self.admin.run_command("Repository check", ["check", *choice])

    async def do_init(self) -> None:
        ok, _ = await self.admin.confirm("Create the repository", "Creates the restic repository at the configured "
                                         "repo_path if it does not exist yet (nothing happens if it does).", confirm="Create")
        if not ok:
            return
        await self.admin.run_command("Create the repository", ["init-repo"])
        await self.reload()

    async def do_job_filter(self) -> None:
        cfg = await asyncio.to_thread(config.load_config)
        jobs, _ = await asyncio.to_thread(config.load_jobs, cfg)
        options = [("All jobs", "")] + [(job.name, job.job_id) for job in jobs]
        choice = await self.admin.pick("Show snapshots of...", options)
        if choice is None:
            return
        self._job = choice or None
        self._job_name = next((job.name for job in jobs if job.job_id == choice), None)
        await self.reload()

    # ---------------------------------------------------------------- bindings and buttons

    def action_ls(self) -> None:
        self.admin.spawn(self.do_ls)

    def action_restore(self) -> None:
        self.admin.spawn(self.do_restore)

    def action_find(self) -> None:
        self.admin.spawn(self.do_find)

    def action_check(self) -> None:
        self.admin.spawn(self.do_check)

    def action_init(self) -> None:
        self.admin.spawn(self.do_init)

    def action_job_filter(self) -> None:
        self.admin.spawn(self.do_job_filter)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
