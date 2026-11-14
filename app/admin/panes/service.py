"""Service tab: state of the systemd service and timer, controls, reinstalling the units, and the live journal."""

import asyncio
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, RichLog, Static
from textual.widgets.data_table import RowDoesNotExist

from .. import service
from ..app import Pane
from ..logs import audit

REFRESH_S = 5


class ServicePane(Pane):
    TAB_ID = "service"
    TAB_TITLE = "5 Service"
    ACTIONS = {
        "service.start": "do_start", "service.stop": "do_stop", "service.enable": "do_enable",
        "service.disable": "do_disable", "service.logs": "do_logs", "service.install": "do_install",
    }
    BINDINGS = [
        Binding("u", "start", "Start"), Binding("p", "stop", "Stop"), Binding("a", "enable", "Enable"),
        Binding("x", "disable", "Disable"), Binding("l", "logs", "Follow journal"), Binding("i", "install", "Reinstall"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._journal: Optional[asyncio.subprocess.Process] = None
        self._journal_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Static("", id="service-note", markup=False, classes="note")
        yield DataTable(id="units", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Start (run a tick now)", id="start")
            yield Button("Stop", id="stop", variant="warning")
            yield Button("Enable timer", id="enable")
            yield Button("Disable timer", id="disable", variant="warning")
            yield Button("Follow journal", id="logs")
            yield Button("Reinstall units", id="install")
        yield RichLog(id="journal", wrap=True, markup=False, max_lines=2000)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Unit", "Name", "State", "Enabled", "Since", "Next run", "Last run")
        self.set_interval(REFRESH_S, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.admin.active_pane() is self:
            self.admin.spawn(self.reload)

    async def on_unmount(self) -> None:
        await self._stop_journal()

    def _selected_unit(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#service-note", Static)
        table = self.query_one(DataTable)
        if not await asyncio.to_thread(service.available):
            table.clear()
            note.update("systemd is not available on this host. Run `python -m app tick` from cron or another scheduler.")
            return
        base = await self.admin.service_base()
        if base is None:
            table.clear()
            note.update("No systemd unit for this install was found. Run `sudo deploy/install.sh`, or start the console "
                        "with --unit NAME if it was installed under another name.")
            return
        note.update(f"Units for {base}. The timer starts the service every 5 minutes; the service runs due jobs. "
                    "Changing them uses sudo, which asks for your password.")
        keep = self._selected_unit()
        rows = []
        for label, unit in service.unit_names(base):
            rows.append((label, unit, await asyncio.to_thread(service.unit_status, unit)))
        table.clear()
        for label, unit, status in rows:
            table.add_row(unit, label, status["active"], status["enabled"], status["since"], status["next"],
                          status["last"], key=unit)
        if keep:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:
                pass

    # ---------------------------------------------------------------- actions

    async def _control(self, action: str, warning: Optional[str] = None) -> None:
        unit = self._selected_unit()
        if unit is None:
            await self.admin.show("No unit", "Select a unit first.")
            return
        if warning:
            ok, _ = await self.admin.confirm(f"{action.capitalize()} {unit}", warning, confirm=action.capitalize(),
                                             danger=True)
            if not ok:
                return
        code = await self.admin.run_systemctl(action, unit)
        audit(f"service {action}", unit=unit, exit=code)
        await self.reload()
        if code != 0:
            await self.admin.error(f"systemctl {action} {unit} exited with status {code}.")

    async def do_start(self) -> None:
        await self._control("start")

    async def do_stop(self) -> None:
        await self._control("stop", "Stopping the service interrupts a running backup. Stopping the timer pauses "
                                    "scheduled backups until it is started again.")

    async def do_enable(self) -> None:
        await self._control("enable")

    async def do_disable(self) -> None:
        await self._control("disable", "Disabling the timer stops scheduled backups (and the heartbeat to the dashboard) "
                                       "until it is enabled again.")

    async def do_install(self) -> None:
        await self.admin.reinstall_units()
        await self.reload()

    async def _stop_journal(self) -> None:
        if self._journal_task is not None:
            self._journal_task.cancel()
            self._journal_task = None
        if self._journal is not None and self._journal.returncode is None:
            self._journal.terminate()
            await self._journal.wait()
        self._journal = None

    async def _follow(self, unit: str) -> None:
        log = self.query_one("#journal", RichLog)
        try:
            self._journal = await asyncio.create_subprocess_exec(
                *service.journal_command(unit), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            log.write(f"cannot run journalctl: {exc}")
            return
        if self._journal.stdout is None:
            return
        async for line in self._journal.stdout:
            log.write(line.decode("utf-8", "replace").rstrip())

    async def do_logs(self) -> None:
        if self._journal_task is not None:
            await self._stop_journal()
            self.notify("Stopped following the journal")
            return
        unit = self._selected_unit()
        if unit is None:
            await self.admin.show("No unit", "Select a unit first.")
            return
        log = self.query_one("#journal", RichLog)
        log.clear()
        log.write(f"--- journal of {unit} (press l to stop) ---")
        self._journal_task = asyncio.create_task(self._follow(unit))

    # ---------------------------------------------------------------- bindings and buttons

    def action_start(self) -> None:
        self.admin.spawn(self.do_start)

    def action_stop(self) -> None:
        self.admin.spawn(self.do_stop)

    def action_enable(self) -> None:
        self.admin.spawn(self.do_enable)

    def action_disable(self) -> None:
        self.admin.spawn(self.do_disable)

    def action_install(self) -> None:
        self.admin.spawn(self.do_install)

    def action_logs(self) -> None:
        self.admin.spawn(self.do_logs)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
