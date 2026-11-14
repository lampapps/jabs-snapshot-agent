"""Run a command of this agent in a child process and stream its output, with a Stop button."""

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Static

CODE_DIR = Path(__file__).resolve().parents[2]
GRACE_S = 10


def child_command(args: list) -> list:
    """`python -m app ARGS...` with the interpreter this console runs on."""
    return [sys.executable, "-m", "app", *args]


class RunScreen(ModalScreen):
    """Shows a child process's output as it runs. Returns its exit code (negative when a signal ended it)."""

    BINDINGS = [Binding("escape", "stop", "Stop / close")]

    def __init__(self, title: str, command: list, note: str = ""):
        super().__init__()
        self._title, self._command, self._note = title, command, note
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stops = 0
        self._code: Optional[int] = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog run"):
            yield Label(self._title, classes="dialog-title")
            if self._note:
                yield Static(self._note, markup=False, classes="help")
            yield RichLog(id="run-output", wrap=True, markup=False, max_lines=5000)
            yield Static("Starting...", id="run-status", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Stop", id="stop", variant="warning")
                yield Button("Close", id="close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#stop").focus()
        self.run_worker(self._run(), group="run", exit_on_error=False)

    def _signal(self, sig: int) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    async def _run(self) -> None:
        log = self.query_one("#run-output", RichLog)
        status = self.query_one("#run-status", Static)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=CODE_DIR, env=env, start_new_session=True)
        except OSError as exc:
            log.write(f"cannot start {self._command[0]}: {exc}")
            status.update("Could not start")
            self._finish(-1)
            return
        status.update(f"Running (pid {self._proc.pid}). Stop sends an interrupt so the run can clean up.")
        try:
            assert self._proc.stdout is not None
            async for line in self._proc.stdout:
                log.write(line.decode("utf-8", "replace").rstrip())
            code = await self._proc.wait()
        finally:
            if self._proc.returncode is None:  # the console is closing: never leave the run behind
                self._signal(signal.SIGTERM)
        stopped = self._stops > 0
        status.update(f"{'Stopped' if stopped else 'Finished'}: exit status {code}"
                      + (" (success)" if code == 0 else " (FAILED)" if not stopped else ""))
        self._finish(code)

    def _finish(self, code: int) -> None:
        self._code = code
        self.query_one("#stop", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()

    def _stop(self) -> None:
        """First press interrupts, the second terminates, the third kills."""
        self._stops += 1
        sig = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)[min(self._stops, 3) - 1]
        self._signal(sig)
        self.query_one("#run-status", Static).update(f"Stopping ({sig.name} sent)...")
        if self._stops == 1:
            self.set_timer(GRACE_S, lambda: self._escalate())

    def _escalate(self) -> None:
        if self._proc is not None and self._proc.returncode is None and self._stops == 1:
            self._stop()

    def action_stop(self) -> None:
        if self._code is not None:
            self.dismiss(self._code)
        else:
            self._stop()

    @on(Button.Pressed, "#stop")
    def _stop_pressed(self) -> None:
        self._stop()

    @on(Button.Pressed, "#close")
    def _close_pressed(self) -> None:
        self.dismiss(self._code)
