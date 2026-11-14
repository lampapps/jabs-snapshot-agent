"""Reading and controlling the systemd units that run the agent."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

PROPERTIES = ("ActiveState", "SubState", "UnitFileState", "ActiveEnterTimestamp", "NextElapseUSecRealtime",
              "LastTriggerUSec", "Result")
ACTIONS = ("start", "stop", "restart", "enable", "disable")
UNIT_DIR = Path("/etc/systemd/system")
CODE_DIR = Path(__file__).resolve().parents[2]


def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)


def available() -> bool:
    """True on a host running systemd."""
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def _show(unit: str, *properties: str) -> dict:
    args = ["systemctl", "show", unit, "--no-pager"]
    for prop in properties:
        args += ["-p", prop]
    try:
        result = _run(args)
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (line.partition("=") for line in result.stdout.splitlines())
    return {key: value for key, _, value in pairs if key}


def detect_base(override: Optional[str] = None) -> Optional[str]:
    """The unit name this install uses (e.g. 'jabs-snapshot-agent'), from `override` or by looking for the
    jabs-snapshot-agent* service whose WorkingDirectory is this install."""
    if override:
        return override.removesuffix(".service")
    if not available():
        return None
    try:
        listing = _run(["systemctl", "list-unit-files", "jabs-snapshot-agent*.service", "--no-legend", "--no-pager"])
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.split()[0].removesuffix(".service") for line in listing.stdout.splitlines() if line.strip()]
    for name in names:
        if _show(f"{name}.service", "WorkingDirectory").get("WorkingDirectory") == str(CODE_DIR):
            return name
    return names[0] if len(names) == 1 else None


def unit_names(base: str) -> list:
    """[(label, unit)] for the tick service (run by the timer) and the timer."""
    return [("Tick service", f"{base}.service"), ("Timer", f"{base}.timer")]


def unit_status(unit: str) -> dict:
    """{'active', 'enabled', 'since', 'next', 'last'} as text for one unit; '-' when unknown."""
    props = _show(unit, *PROPERTIES)
    if not props:
        return {"active": "unknown", "enabled": "-", "since": "-", "next": "-", "last": "-"}
    active = props.get("ActiveState", "unknown")
    if props.get("SubState") and props["SubState"] != active:
        active = f"{active} ({props['SubState']})"
    if props.get("Result") not in (None, "", "success"):
        active += f", last result {props['Result']}"
    return {"active": active, "enabled": props.get("UnitFileState") or "-",
            "since": props.get("ActiveEnterTimestamp") or "-", "next": props.get("NextElapseUSecRealtime") or "-",
            "last": props.get("LastTriggerUSec") or "-"}


def control_command(action: str, unit: str) -> list:
    """The command that changes a unit's state; enable and disable also start and stop it. sudo is added for
    non-root users."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action '{action}'")
    if action in ("start", "stop", "restart"):
        command = ["systemctl", action, unit]
    else:
        command = ["systemctl", action, "--now", unit]
    return command if os.geteuid() == 0 else ["sudo", *command]


def journal_command(unit: str) -> list:
    return ["journalctl", "-u", unit, "-f", "-n", "200", "-o", "short-iso", "--no-pager"]


def installed_options(base: str) -> dict:
    """{'user', 'read_all'} read from the installed unit file, so a reinstall keeps the choices made earlier."""
    options = {"user": "", "read_all": False}
    try:
        text = (UNIT_DIR / f"{base}.service").read_text(encoding="utf-8")
    except OSError:
        return options
    for line in text.splitlines():
        if line.startswith("User="):
            options["user"] = line.partition("=")[2].strip()
        elif line.startswith("AmbientCapabilities=") and "CAP_DAC_READ_SEARCH" in line:
            options["read_all"] = True
    return options


def install_command(base: str, user: str, read_all: bool) -> list:
    """The command that (re)installs the units from this install's deploy/install.sh."""
    command = [str(CODE_DIR / "deploy" / "install.sh"), "--name", base]
    if user:
        command += ["--user", user]
    if read_all:
        command.append("--read-all")
    return command if os.geteuid() == 0 else ["sudo", *command]
