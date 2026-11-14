"""Operations shared by the command line and the admin console, so both behave the same."""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import portalocker

from . import config, restic, runner, schedule, search, settings, state, storage
from .config import Config, ConfigError

logger = logging.getLogger(__name__)


def credentials() -> tuple:
    password, password_file = settings.restic_password()
    if not password and not password_file:
        raise ConfigError("neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set (put one in .env)")
    return password, password_file


def repo_path(cfg: Config) -> str:
    if not cfg.repo_path:
        raise ConfigError(f"no repo_path in {settings.CONFIG_PATH}")
    return cfg.repo_path


# ---------------------------------------------------------------- repository

def init_repository(cfg: Config) -> bool:
    """Create the repository unless it exists; True when it was created."""
    repo, (password, password_file) = repo_path(cfg), credentials()
    global_args = restic.global_args_from(cfg.restic_options)
    if restic.repo_exists(repo, password, password_file, global_args=global_args):
        return False
    restic.init_repo(repo, password, password_file, global_args=global_args)
    return True


def check_label(deep: bool = False, subset: Optional[str] = None) -> str:
    deep = deep or bool(subset)
    return (f"partial data check (subset={subset})" if subset else "full data check" if deep
            else "quick check (metadata only)")


def check_repository(cfg: Config, deep: bool = False, subset: Optional[str] = None) -> None:
    """Verify the repository; raises ResticError when it is damaged."""
    repo, (password, password_file) = repo_path(cfg), credentials()
    restic.check(repo, password, password_file, read_data=(deep or bool(subset)) and not subset,
                 read_data_subset=subset, global_args=restic.global_args_from(cfg.restic_options))


def snapshot_list(cfg: Config, job_name: Optional[str] = None) -> list:
    repo, (password, password_file) = repo_path(cfg), credentials()
    tags = config.find_job(job_name, cfg).tags if job_name else None
    return restic.list_snapshots(repo, tags=tags, password=password, password_file=password_file,
                                 global_args=restic.global_args_from(cfg.restic_options))


def snapshot_fields(snap: dict) -> tuple:
    """(short id, time, host, tags, paths) of a snapshot as display text."""
    stamp = (snap.get("time") or "")[:19].replace("T", " ")
    return (snap.get("short_id", snap.get("id", "")[:8]), stamp, snap.get("hostname", ""),
            ",".join(snap.get("tags") or []) or "-", " ".join(snap.get("paths") or []))


def snapshot_files(cfg: Config, snapshot: str, path: Optional[str] = None) -> list:
    repo, (password, password_file) = repo_path(cfg), credentials()
    entries = restic.list_files(repo, snapshot, path=path, password=password, password_file=password_file,
                                global_args=restic.global_args_from(cfg.restic_options))
    return [entry["path"] for entry in entries if entry.get("path")]


def find_matches(cfg: Config, query: str) -> list:
    repo, (password, password_file) = repo_path(cfg), credentials()
    return search.find_paths(repo, query, password, password_file, restic.global_args_from(cfg.restic_options))


def restore_snapshot(cfg: Config, snapshot: str, target: str, path: Optional[str] = None) -> None:
    repo, (password, password_file) = repo_path(cfg), credentials()
    restic.restore(repo, snapshot, target, include_path=path, password=password, password_file=password_file,
                   global_args=restic.global_args_from(cfg.restic_options))


# ---------------------------------------------------------------- jobs

def format_when(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def format_last(last: Optional[dict]) -> str:
    """'ok 2027-01-15 03:00' / 'FAILED ...' / '-' for a job's recorded last run."""
    if not last:
        return "-"
    return f"{'ok' if last.get('success') else 'FAILED'} {format_when(last.get('finished_at'))}"


def job_lock_info(name: str) -> Optional[dict]:
    """The holder's details when the job is running right now, else None. Probes only while a lock file exists."""
    path = runner.lock_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "a+", encoding="utf-8") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                handle.seek(0)
                try:
                    return json.load(handle) or {}
                except ValueError:
                    return {}
            portalocker.unlock(handle)
    except OSError:
        return None
    return None


def jobs_overview(cfg: Config, now: Optional[datetime] = None) -> tuple:
    """([{job, next, last, running}], errors) for every job file; `next` is a datetime or None."""
    jobs, errors = config.load_jobs(cfg)
    data = state.read()
    now = now or schedule.local_now()
    rows = [{"job": job, "next": schedule.next_run(job, now),
             "last": data.get("jobs", {}).get(job.name, {}).get("last_run"), "running": job_lock_info(job.name)}
            for job in jobs]
    return rows, errors


def delete_job(job: config.Job) -> None:
    """Delete a job's file from config/jobs and forget its schedule state. Snapshots in the repository are kept.

    Refuses to delete a file outside the jobs directory, or a job that is running.
    """
    path = Path(job.path).resolve()
    if path.parent != settings.JOBS_DIR.resolve() or path.suffix not in (".yaml", ".yml"):
        raise ConfigError(f"{job.path} is not a file in {settings.JOBS_DIR}; delete it by hand")
    if job_lock_info(job.name) is not None:
        raise ConfigError(f"'{job.name}' is running; wait for it to finish")
    path.unlink()
    try:
        with state.transaction() as data:
            data.get("jobs", {}).pop(job.name, None)
    except (OSError, portalocker.exceptions.LockException) as exc:
        logger.warning("deleted %s but could not clear its schedule state: %s", path.name, exc)


def reset_state() -> int:
    """Delete lock files and the schedule state; returns how many files went. The repository is never touched."""
    removed = 0
    for path in [*settings.LOCK_DIR.glob("*.lock"), *settings.STATE_PATH.parent.glob("state.*")]:
        path.unlink()
        removed += 1
    if settings.LOCK_DIR.is_dir() and not any(settings.LOCK_DIR.iterdir()):
        shutil.rmtree(settings.LOCK_DIR, ignore_errors=True)
    return removed


def writable_paths(cfg: Config) -> list:
    """Directories the agent must be able to write; deploy/install.sh turns them into ReadWritePaths."""
    jobs, _ = config.load_jobs(cfg)
    wanted = {str(settings.DATA_DIR)}
    for repo in {job.repo_path for job in jobs} | ({cfg.repo_path} if cfg.repo_path else set()):
        if storage.is_local_repo(repo):
            wanted.add(repo)
    caches = {cfg.restic_options.cache_dir} | {job.restic_options.cache_dir for job in jobs}
    wanted |= {path for path in caches if path}
    if not any(job.restic_options.no_cache for job in jobs) and not cfg.restic_options.no_cache:
        wanted.add(str(Path.home() / ".cache" / "restic"))
    return sorted(wanted)


def storage_samples(cfg: Config) -> list:
    """Measure storage now (volume, repository size and each job's source size)."""
    jobs, _ = config.load_jobs(cfg)
    repo_paths = sorted({job.repo_path for job in jobs} or ({cfg.repo_path} if cfg.repo_path else set()))
    samples = storage.collect_scheduled(cfg, repo_paths, force=True)
    if cfg.storage.source_size:
        samples += [s for s in (storage.source_size_sample(cfg, job) for job in jobs) if s]
    return samples
