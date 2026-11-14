"""Editing a file in the system editor with validation, revert and follow-up, shared by the Jobs and Files tabs."""

import asyncio
from pathlib import Path
from typing import Optional

from .. import config, ops
from .checks import check_file, managed_files, revert, snapshot, starter_text
from .editor import EditorError
from .logs import audit


def _writable_paths() -> Optional[list]:
    """The directories the service must be able to write, or None while the configuration is unusable."""
    try:
        return ops.writable_paths(config.load_config())
    except config.ConfigError:
        return None


async def edit_entry(app, entry: dict) -> bool:
    """Open `entry` in the editor and check it afterwards (edit again, revert or keep when invalid).

    Returns True when the file changed. Changes apply at the next tick; when the directories the sandboxed service
    may write change, offers to reinstall the units.
    """
    path = entry["path"]
    before = snapshot(path)
    paths_before = await asyncio.to_thread(_writable_paths)
    created = False
    if before is None:
        starter = starter_text(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        if starter is not None:
            path.write_text(starter, encoding="utf-8")
            created = True
    while True:
        try:
            await app.run_editor(path)
        except EditorError as exc:
            if created:
                revert(path, before)
            await app.error(str(exc))
            return False
        result = await asyncio.to_thread(check_file, entry)
        if result["state"] != "error":
            break
        choice = await app.choose(
            f"{entry['label']} is not valid", result["message"],
            [("Edit again", "again"), ("Revert my changes", "revert"), ("Keep it anyway", "keep")])
        if choice == "again":
            continue
        if choice == "keep":
            break
        revert(path, before)
        audit("file edit reverted", file=entry["label"])
        await app.show("Reverted", f"{path} was put back as it was before the edit.")
        return False
    if entry["kind"] == "env" and before is None and path.exists():
        path.chmod(0o600)
    if snapshot(path) == before:
        return False
    audit("file edited", file=entry["label"])
    app.notify(f"{entry['label']} saved; it applies at the next run")
    paths_after = await asyncio.to_thread(_writable_paths)
    if paths_before is not None and paths_after is not None and paths_before != paths_after:
        added = sorted(set(paths_after) - set(paths_before))
        removed = sorted(set(paths_before) - set(paths_after))
        lines = [f"+ {p}" for p in added] + [f"- {p}" for p in removed]
        ok, _ = await app.confirm(
            "The service's writable directories changed",
            "The systemd service is sandboxed and may only write to the directories it was installed with:\n\n"
            + "\n".join(lines) + "\n\nReinstall the units now so backups can write there?", confirm="Reinstall now")
        if ok:
            await app.reinstall_units()
    return True


def entry_for_path(path) -> Optional[dict]:
    """The managed-file entry for a file under config/, or None."""
    return next((e for e in managed_files() if e["path"] == Path(path)), None)
