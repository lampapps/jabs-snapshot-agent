"""Launching the system editor for the configuration files."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path


class EditorError(Exception):
    """No usable editor, or it could not be started."""


def find_editor() -> list:
    """The editor command: $VISUAL, $EDITOR, then the system default (sensible-editor, editor) and common editors."""
    for variable in ("VISUAL", "EDITOR"):
        value = os.environ.get(variable, "").strip()
        if value:
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            if parts and shutil.which(parts[0]):
                return parts
    for name in ("sensible-editor", "editor", "nano", "vim", "vi"):
        found = shutil.which(name)
        if found:
            return [found]
    return []


def run_editor(path: Path) -> int:
    """Open `path` in the editor and wait; the caller must have released the terminal first."""
    editor = find_editor()
    if not editor:
        raise EditorError("no editor found; set the EDITOR environment variable (for example EDITOR=nano)")
    try:
        return subprocess.run([*editor, str(path)], check=False).returncode
    except OSError as exc:
        raise EditorError(f"cannot start {editor[0]}: {exc}") from exc
