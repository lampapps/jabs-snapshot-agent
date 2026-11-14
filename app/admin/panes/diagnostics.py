"""Diagnostics tab: run the tick, test email and the dashboard link, measure storage, and inspect or reset state."""

import asyncio
import json

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Static

from ... import config, ops, state
from ...dashboard import Client
from ..app import Pane
from ..logs import audit


class DiagnosticsPane(Pane):
    TAB_ID = "diagnostics"
    TAB_TITLE = "4 Diagnostics"
    ACTIONS = {
        "diagnostics.tick": "do_tick", "diagnostics.testmail": "do_testmail", "diagnostics.storage": "do_storage",
        "diagnostics.paths": "do_paths", "diagnostics.reset": "do_reset",
        "diagnostics.dashboard": "do_dashboard", "diagnostics.state": "do_state",
    }
    BINDINGS = [
        Binding("t", "tick", "Run tick"), Binding("m", "testmail", "Test mail"), Binding("s", "storage", "Storage"),
        Binding("h", "dashboard", "Dashboard link"), Binding("e", "state", "State"), Binding("p", "paths", "Paths"),
        Binding("z", "reset", "Reset state"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Everything here uses the same code as the timer and the command line.", markup=False,
                     classes="note")
        with Horizontal(classes="buttons"):
            yield Button("Run tick now", id="tick", variant="primary")
            yield Button("Test mail", id="testmail")
            yield Button("Measure storage", id="storage")
            yield Button("Dashboard link", id="dashboard")
        with Horizontal(classes="buttons"):
            yield Button("Show state", id="state")
            yield Button("Writable paths", id="paths")
            yield Button("Reset state", id="reset", variant="error")

    async def reload(self) -> None:
        return None

    # ---------------------------------------------------------------- actions

    async def do_tick(self) -> None:
        ok, _ = await self.admin.confirm(
            "Run the tick now", "Does what the timer does every 5 minutes: sends the heartbeat, runs every job that is "
            "due (a real backup), and reports storage. Nothing runs if no job is due.", confirm="Run now")
        if not ok:
            return
        audit("tick run manually")
        await self.admin.run_command("Tick", ["tick"])

    async def do_testmail(self) -> None:
        await self.admin.run_command("Test mail", ["testmail"])

    async def do_storage(self) -> None:
        choice = await self.admin.choose(
            "Measure storage", "Measures the repository volume, the repository size and each job's source size. "
            "The repository size can take a while on large repositories.",
            [("Measure and send to the dashboard", []), ("Measure only (--print)", ["--print"]), ("Cancel", None)])
        if choice is None:
            return
        await self.admin.run_command("Storage", ["report-storage", *choice])

    async def do_dashboard(self) -> None:
        cfg = await asyncio.to_thread(config.load_config)
        client = Client.from_env(cfg)
        if not client.enabled:
            await self.admin.show("Dashboard link", f"{client.describe()}\n\nSet JABS_DASHBOARD_URL and JABS_AGENT_KEY in "
                                  ".env (Files tab) and make sure dashboard.enabled is not false in config.yaml.")
            return
        sent = await asyncio.to_thread(client.send_scheduler_check, 0)
        audit("dashboard heartbeat test", sent=sent)
        await self.admin.show(
            "Dashboard link", f"{client.describe()}\n\n"
            + ("A heartbeat was accepted." if sent else "The heartbeat was NOT accepted. Check the URL and key, that "
               "the agent is registered on the dashboard, and that the dashboard is reachable."), error=not sent)

    async def do_state(self) -> None:
        data = await asyncio.to_thread(state.read)
        locks = {}
        cfg = await asyncio.to_thread(config.load_config)
        rows, _ = await asyncio.to_thread(ops.jobs_overview, cfg)
        for row in rows:
            if row["running"] is not None:
                locks[row["job"].name] = row["running"]
        body = json.dumps({"state": data, "running": locks}, indent=1, sort_keys=True, default=str)
        await self.admin.show("Schedule state", body or "{}")

    async def do_paths(self) -> None:
        cfg = await asyncio.to_thread(config.load_config)
        paths = await asyncio.to_thread(ops.writable_paths, cfg)
        await self.admin.show("Directories the service may write", "\n".join(paths) + "\n\nThe systemd service is "
                              "sandboxed to these. After changing repo_path or a cache directory, reinstall the units "
                              "(Service tab).")

    async def do_reset(self) -> None:
        ok, _ = await self.admin.confirm(
            "Reset schedule state", "Deletes the lock files and the schedule state (which slots were served, each job's "
            "last result). The repository and your config are untouched. At the next tick every job is treated as seen "
            "for the first time, so none runs until its next slot. Do not do this while a backup is running.",
            confirm="Reset", danger=True, type_word="RESET")
        if not ok:
            return
        removed = await asyncio.to_thread(ops.reset_state)
        audit("state reset", files=removed)
        await self.admin.show("Reset", f"Removed {removed} file(s).")

    # ---------------------------------------------------------------- bindings and buttons

    def action_tick(self) -> None:
        self.admin.spawn(self.do_tick)

    def action_testmail(self) -> None:
        self.admin.spawn(self.do_testmail)

    def action_storage(self) -> None:
        self.admin.spawn(self.do_storage)

    def action_dashboard(self) -> None:
        self.admin.spawn(self.do_dashboard)

    def action_state(self) -> None:
        self.admin.spawn(self.do_state)

    def action_paths(self) -> None:
        self.admin.spawn(self.do_paths)

    def action_reset(self) -> None:
        self.admin.spawn(self.do_reset)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
