"""A small JSON file for what restic cannot tell us: schedule slots already served, last results, report times."""

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Iterator, Optional

import portalocker

from . import settings

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_S = 10


def _path(path: Optional[Path]) -> Path:
    return Path(path or settings.STATE_PATH)


def read(path: Optional[Path] = None) -> dict:
    """The current state; empty when the file is missing or unreadable."""
    try:
        with open(_path(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("state file unreadable, starting fresh: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def transaction(path: Optional[Path] = None) -> Iterator[dict]:
    """Read-modify-write under a file lock; the file is written only if the block raises nothing."""
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(target.with_suffix(".lock")), mode="a", timeout=LOCK_TIMEOUT_S):
        data = read(target)
        yield data
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, target)


def job_entry(data: dict, job_id: str) -> dict:
    """One job's entry; jobs are keyed by their id, so renaming a job keeps its state."""
    return data.setdefault("jobs", {}).setdefault(job_id, {})


def record_run(job_id: str, result: dict, path: Optional[Path] = None) -> None:
    """Remember a job's latest outcome so `jobs` can show it without a dashboard."""
    try:
        with transaction(path) as data:
            job_entry(data, job_id)["last_run"] = result
    except (OSError, portalocker.exceptions.LockException) as exc:
        logger.warning("could not record the result of %s: %s", job_id, exc)
