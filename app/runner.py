"""Run one backup job: lock, backup, quick check, prune, and report to the dashboard, email and Uptime Kuma."""

import logging
import time
import uuid

from . import kuma, locking, mailer, restic, settings, state, storage
from .config import Config, Job
from .dashboard import Client
from .restic import ResticError

BACKUP_TYPE = "snapshot"
PROGRESS_POST_INTERVAL_S = 5

logger = logging.getLogger(__name__)


class _JobLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"{self.extra['job']} - {msg}", kwargs


def lock_path(job_id: str) -> str:
    """The lock file of a job, named by its id so renaming a job never changes it."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_id)
    return str(settings.LOCK_DIR / f"{safe}.lock")


def run_job(job: Job, cfg: Config, client: Client, do_init: bool = False, do_prune: bool = False,
            dry_run: bool = False) -> bool:
    """Run one job; True on success. Monitoring, email and storage reporting never change the outcome."""
    log = _JobLogger(logger, {"job": job.name})
    password, password_file = settings.restic_password()
    if not password and not password_file:
        log.error("Neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set in .env")
        return False

    try:
        lock_file = locking.acquire_lock(lock_path(job.job_id))
    except RuntimeError as exc:
        log.error("Could not start backup: %s", exc)
        return False

    start = time.time()
    try:
        result = _backup(job, cfg, client, log, password, password_file, do_init, do_prune, dry_run, start)
    finally:
        locking.release_lock(lock_file)

    state.record_run(job.job_id, {**result, "dry_run": dry_run})
    kuma.ping("up" if result["success"] else "down",
              f"{'[DRY RUN] ' if dry_run else ''}{job.name}: {'OK' if result['success'] else 'FAILED'}",
              dry_run=dry_run)
    return result["success"]


def _backup(job, cfg, client, log, password, password_file, do_init, do_prune, dry_run, start) -> dict:
    """The locked part of a run; returns the outcome recorded in the state file."""
    run_id = str(uuid.uuid4())
    # A snapshot job has one target, identified like the job by its id, so a rename never splits its history.
    target_id, target_label = job.job_id, job.name
    global_args = restic.global_args_from(job.restic_options)
    tag = " [DRY RUN] " if dry_run else ""
    finished = {"success": False, "finished_at": start, "duration": 0.0, "error": None, "snapshot_id": None}

    def fail(message: str, duration: float) -> dict:
        client.send_backup_complete(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, duration, run_id=run_id,
                                    success=False, error_message=message, dry_run=dry_run)
        return {**finished, "finished_at": time.time(), "duration": duration, "error": message}

    try:
        client.send_backup_start(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, run_id=run_id, dry_run=dry_run)
        log.info("###### Starting backup: %s (%s) ######", job.name, settings.machine_hostname(cfg.hostname))

        if not restic.repo_exists(job.repo_path, password, password_file, global_args=global_args):
            if not do_init:
                log.error("Repository does not exist: %s. Run `python -m app init-repo` or pass --init.",
                          job.repo_path)
                return fail(f"Repository does not exist: {job.repo_path}", time.time() - start)
            log.info("Initializing new restic repository: %s", job.repo_path)
            client.send_backup_stage(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, "Initializing repository",
                                     run_id=run_id, dry_run=dry_run)
            restic.init_repo(job.repo_path, password, password_file, global_args=global_args)

        client.send_backup_stage(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, "Running backup", run_id=run_id,
                                 dry_run=dry_run)
        progress = {"last_post": 0.0, "last_decile": -1}

        def on_progress(event: dict) -> None:
            done = event.get("percent_done")
            if done is None:
                return
            percent = int(done * 100)
            eta = event.get("seconds_remaining")
            if percent // 10 > progress["last_decile"]:
                progress["last_decile"] = percent // 10
                log.info("Progress: %d%%%s", percent, f", eta {eta}s" if eta is not None else "")
            now = time.time()
            if now - progress["last_post"] >= PROGRESS_POST_INTERVAL_S:
                progress["last_post"] = now
                client.send_backup_progress(job.job_id, job.name, BACKUP_TYPE, target_id, run_id=run_id,
                                            percent_complete=percent, eta_seconds=eta,
                                            bytes_backed_up=event.get("bytes_done"))

        summary = restic.backup(
            job.repo_path, job.paths, tags=job.tags, exclude_patterns=job.exclude, password=password,
            password_file=password_file, dry_run=dry_run, global_args=global_args, on_progress=on_progress,
            **restic.backup_kwargs_from(job.restic_options))

        duration = time.time() - start
        snapshot_id = summary.get("snapshot_id", "")
        short_id = snapshot_id[:8]
        files = summary.get("files_new", 0) + summary.get("files_changed", 0)
        added = summary.get("data_added", 0)
        log.info("Backup complete: snapshot=%s (%s) files=%s data_added=%s duration=%.1fs",
                 short_id, snapshot_id, files, added, duration)
        client.send_backup_complete(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, duration, run_id=run_id,
                                    files_backed_up=files, bytes_backed_up=added, success=True, dry_run=dry_run,
                                    snapshot_short_id=short_id, snapshot_id=snapshot_id)
        mailer.notify(cfg.email, "backup_complete", f"JABS snapshot backup complete: {job.name}",
                      f"{tag}Backup of {job.name} completed in {duration:.1f}s "
                      f"(snapshot {short_id}, {files} files changed).")
        finished = {**finished, "success": True, "finished_at": time.time(), "duration": duration,
                    "snapshot_id": snapshot_id, "files": files, "bytes_added": added}

        if job.verify_after_backup:
            _quick_check(job, cfg, client, log, password, password_file, global_args, run_id, target_id,
                         target_label, dry_run, tag)
        pruned = False
        if do_prune or job.prune_after_backup:
            pruned = _prune(job, client, log, password, password_file, global_args, run_id, target_id,
                            target_label, dry_run)
        if not dry_run:
            _report_storage(cfg, job, client, log, repo_size=pruned)
        return finished

    except ResticError as exc:
        duration = time.time() - start
        log.error("Backup failed: %s", exc)
        mailer.notify(cfg.email, "error", f"JABS snapshot backup FAILED: {job.name}",
                      f"{tag}Backup of {job.name} failed after {duration:.1f}s: {exc}")
        return fail(str(exc), duration)


def _quick_check(job, cfg, client, log, password, password_file, global_args, run_id, target_id, target_label,
                 dry_run, tag) -> None:
    try:
        log.info("Running quick repository check (metadata only)")
        client.send_backup_stage(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, "Verifying repository (quick check)",
                                 run_id=run_id, dry_run=dry_run)
        restic.check(job.repo_path, password, password_file, global_args=global_args)
        log.info("Quick repository check passed")
        client.send_check_result(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, run_id, success=True,
                                 dry_run=dry_run)
    except ResticError as exc:
        # The backup just taken is still good, but a failing check is worth an alert.
        log.error("Quick repository check FAILED: %s", exc)
        client.send_check_result(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, run_id, success=False,
                                 error_message=str(exc), dry_run=dry_run)
        mailer.notify(cfg.email, "error", f"JABS snapshot repository check FAILED: {job.name}",
                      f"{tag}Quick repository check after backup of {job.name} failed: {exc}\nRepo: {job.repo_path}")


def _prune(job, client, log, password, password_file, global_args, run_id, target_id, target_label, dry_run) -> bool:
    """Forget and prune this job's old snapshots; True when the repository was pruned."""
    retention = job.retention
    try:
        log.info("Running forget/prune for tags=%s", job.tags)
        client.send_backup_stage(job.job_id, job.name, BACKUP_TYPE, target_id, target_label, "Pruning old snapshots",
                                 run_id=run_id, dry_run=dry_run)
        removed = restic.forget_prune(
            job.repo_path, tags=job.tags, password=password, password_file=password_file, global_args=global_args,
            extra_args=retention.extra_forget_args, keep_last=retention.keep_last, keep_hourly=retention.keep_hourly,
            keep_daily=retention.keep_daily, keep_weekly=retention.keep_weekly,
            keep_monthly=retention.keep_monthly, keep_yearly=retention.keep_yearly,
            keep_within=retention.keep_within or None, group_by=retention.group_by or None)
    except ResticError as exc:
        # A prune failure does not undo a good backup; it is logged only.
        log.warning("forget/prune failed: %s", exc)
        return False
    if removed:
        log.info("Pruned %d snapshot(s): %s", len(removed), removed)
        client.send_target_purged(job.job_id, target_id, removed)
    return True


def _report_storage(cfg: Config, job: Job, client: Client, log, repo_size: bool) -> None:
    """After a backup: the job's source size, and the repository size if it was just pruned."""
    if not client.enabled or not cfg.storage.report:
        return
    try:
        samples = []
        if cfg.storage.source_size:
            samples.append(storage.source_size_sample(cfg, job))
        if repo_size:
            samples.append(storage.repo_size_sample(cfg, job.repo_path))
        client.post_storage([s for s in samples if s])
    except (OSError, ValueError) as exc:
        log.warning("storage report failed: %s", exc)
