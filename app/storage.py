"""Storage samples for the dashboard's storage charts: repo volume space, repo size, per-job source size."""

import logging
import os
import shutil
import time
from typing import Optional

from . import restic, settings, state
from .config import Config, Job
from .restic import ResticError

logger = logging.getLogger(__name__)


def is_local_repo(repo_path: str) -> bool:
    """A plain absolute path (restic's local backend); sftp:, s3:, rest: and friends are remote."""
    return os.path.isabs(repo_path)


def volume_sample(repo_path: str) -> Optional[dict]:
    """Used and total space of the volume holding a local repository."""
    if not is_local_repo(repo_path) or not os.path.isdir(repo_path):
        return None
    try:
        usage = shutil.disk_usage(repo_path)
    except OSError as exc:
        logger.warning("cannot read disk usage of %s: %s", repo_path, exc)
        return None
    return {"name": "Repo volume", "kind": "local", "used_bytes": usage.used, "total_bytes": usage.total}


def repo_size_sample(cfg: Config, repo_path: str) -> Optional[dict]:
    """Size of the restic repository itself (raw data blobs)."""
    password, password_file = settings.restic_password()
    try:
        result = restic.stats(repo_path, mode="raw-data", password=password, password_file=password_file,
                              global_args=restic.global_args_from(cfg.restic_options))
    except ResticError as exc:
        logger.warning("cannot measure the repository: %s", exc)
        return None
    size = result.get("total_size")
    return None if size is None else {"name": "Restic repo", "kind": "repo", "used_bytes": int(size)}


def source_size_sample(cfg: Config, job: Job) -> Optional[dict]:
    """What restoring the job's latest snapshot would write (the size of the data it protects)."""
    password, password_file = settings.restic_password()
    host = job.restic_options.host or settings.machine_hostname()
    try:
        result = restic.stats(
            job.repo_path, mode="restore-size", snapshot="latest", tags=job.tags or None, host=host,
            paths=None if job.tags else job.paths, password=password, password_file=password_file,
            global_args=restic.global_args_from(job.restic_options))
    except ResticError as exc:
        logger.warning("cannot measure the latest snapshot of %s: %s", job.name, exc)
        return None
    if not result.get("snapshots_count"):  # restic exits 0 with zero counts when no snapshot matches
        return None
    size = result.get("total_size")
    return None if size is None else {"name": job.name, "series": "source", "kind": "repo", "used_bytes": int(size)}


def _due(data: dict, key: str, interval_s: float, now: float) -> bool:
    last = data.get("storage", {}).get(key)
    return last is None or now - last >= interval_s


def _mark(data: dict, key: str, now: float) -> None:
    data.setdefault("storage", {})[key] = now


def collect_scheduled(cfg: Config, repo_paths: list, now: Optional[float] = None, force: bool = False) -> list:
    """Volume and repository samples that are due (everything when force), updating the throttle state."""
    now = time.time() if now is None else now
    with state.transaction() as data:  # measuring can take minutes, so the lock is released before it
        want_volume = force or _due(data, "volume", cfg.storage.volume_interval_minutes * 60, now)
        want_repo = force or _due(data, "repo_size", cfg.storage.repo_size_interval_hours * 3600, now)
        if want_volume:
            _mark(data, "volume", now)
        if want_repo:
            _mark(data, "repo_size", now)
    samples = []
    if want_volume:
        samples += [s for s in (volume_sample(path) for path in repo_paths) if s]
    if want_repo:
        samples += [s for s in (repo_size_sample(cfg, path) for path in repo_paths) if s]
    return samples


def describe(samples: list) -> str:
    """Human-readable lines for `report-storage --print`."""
    lines = []
    for s in samples:
        label = s["name"] + (f" ({s['series']})" if s.get("series") else "")
        total = f" of {s['total_bytes']:,}" if s.get("total_bytes") else ""
        lines.append(f"{s['kind']:<5} {label}: {s['used_bytes']:,}{total} bytes")
    return "\n".join(lines) or "nothing to report"
