"""State and validation of the files an administrator edits: config.yaml, .env, the exclude list and the job files."""

import re
import stat
from pathlib import Path
from typing import Optional

import yaml

from .. import config, settings

EXAMPLE_NAME = "config.example.yaml"
TEMPLATE = Path("templates") / "job.yaml"
JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def managed_files() -> list:
    """The editable files in display order; paths are read at call time."""
    files = [
        {"key": "config", "label": "config.yaml", "path": settings.CONFIG_PATH, "kind": "config",
         "note": "settings shared by all jobs: repository, retention, email, storage reporting"},
        {"key": "env", "label": ".env", "path": settings.ENV_PATH, "kind": "env",
         "note": "secrets: restic password, SMTP login, dashboard key"},
        {"key": "exclude", "label": "common_exclude.yaml", "path": settings.COMMON_EXCLUDE_PATH, "kind": "exclude",
         "note": "exclude patterns added to every job"},
    ]
    for path in config.job_files():
        files.append({"key": f"job:{path.name}", "label": f"jobs/{path.name}", "path": path, "kind": "job",
                      "note": "one backup job: paths, tags, schedules and per-job overrides"})
    return files


def mode_text(path: Path) -> str:
    try:
        return format(stat.S_IMODE(path.stat().st_mode), "04o")
    except OSError:
        return "-"


def check_file(entry: dict) -> dict:
    """{'state': 'ok'|'warn'|'error'|'missing', 'message': str} for one managed file. Every file is read at the
    start of each run, so a valid edit takes effect at the next tick."""
    path = entry["path"]
    if not path.exists():
        hint = " (optional)" if entry["kind"] == "exclude" else ""
        return {"state": "missing", "message": f"{path} does not exist{hint}"}
    try:
        if entry["kind"] == "config":
            cfg = config.load_config(path)
            message = f"valid (repository {cfg.repo_path or 'not set'})"
        elif entry["kind"] == "exclude":
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if data is not None and not (isinstance(data, list) and all(isinstance(i, str) for i in data)):
                raise config.ConfigError(f"{path}: must be a list of patterns")
            message = f"valid ({len(data or [])} patterns)"
        elif entry["kind"] == "job":
            job = config.load_job(path, config.load_config())
            crons = ", ".join(job.enabled_crons()) or "no schedule enabled"
            message = f"valid ({len(job.paths)} path(s); {crons})"
        else:
            message = "present"
    except config.ConfigError as exc:
        return {"state": "error", "message": str(exc)}
    except yaml.YAMLError as exc:
        return {"state": "error", "message": f"{path}: not valid YAML: {exc}"}
    except OSError as exc:
        return {"state": "error", "message": f"cannot read {path}: {exc}"}
    if entry["kind"] == "env" and path.stat().st_mode & 0o077:
        return {"state": "warn", "message": f"{message}; readable by other users (chmod 600)"}
    return {"state": "ok", "message": message}


def starter_text(entry: dict) -> Optional[str]:
    """Text a missing file should start with (config.yaml starts as a copy of the example)."""
    if entry["kind"] != "config":
        return None
    example = settings.CONFIG_PATH.parent / EXAMPLE_NAME
    return example.read_text(encoding="utf-8") if example.exists() else None


def new_job_file(name: str) -> Path:
    """Create config/jobs/NAME.yaml from the template (schedules disabled) and return its path."""
    if not JOB_NAME_PATTERN.match(name):
        raise ValueError("a job name uses letters, digits, '.', '_' and '-' (up to 64 characters)")
    target = settings.JOBS_DIR / f"{name}.yaml"
    if target.exists():
        raise ValueError(f"{target.name} already exists")
    template = settings.CONFIG_DIR / TEMPLATE
    if not template.exists():
        raise ValueError(f"the template {template} is missing")
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8").replace('"example"', f'"{name}"'), encoding="utf-8")
    return target


def snapshot(path: Path) -> Optional[bytes]:
    """The file's bytes before an edit, or None if it did not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def revert(path: Path, before: Optional[bytes]) -> None:
    """Put the file back as it was (in place, so its owner and mode stay)."""
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)
