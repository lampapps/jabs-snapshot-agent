#!/usr/bin/env python3
"""JABS Snapshot Agent — CLI entry point for running a single backup job.

Usage:
    python backup.py --job <name-or-path> [--init] [--prune] [--dry-run]
    python backup.py --init                          # init the shared repo only, no job needed
    python backup.py --check                         # quick repo check (metadata only), no job needed
    python backup.py --full-check [--subset 5%]      # deep repo check (reads data), no job needed

    python backup.py --job example                  # config/jobs/example.yaml
    python backup.py --job config/jobs/example.yaml
    python backup.py --job example --init            # init the repo if missing, then run the job
    python backup.py --job example --prune           # forget/prune after backup
    python backup.py --job example --dry-run         # restic --dry-run, still reports to JABS

A cheap `restic check` (metadata only, no data read) runs automatically after
every backup unless disabled with `verify_after_backup: false` in global.yaml
or a job config. `--full-check` (optionally scoped to `--subset`) reads actual
data and is slow, so it's manual-only — see jabs-agent.sh's `check`/`check-deep`
commands.
"""

import argparse
import os
import socket
import sys
import time
import uuid

import yaml
from dotenv import load_dotenv

import restic_client
from restic_client import ResticError
from emailer import process_email_event
from locking import acquire_lock, release_lock
from logger import setup_logger
from monitoring_client import (
    send_backup_start, send_backup_stage, send_backup_complete, send_check_result,
    send_backup_progress, send_target_purged,
)
from settings import CONFIG_DIR, ENV_PATH, GLOBAL_CONFIG_PATH, JOBS_DIR, LOCK_DIR
import uptime_kuma

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars

cli_logger = setup_logger("backup")


def merge_dicts(global_dict, job_dict):
    """Merge two dicts, with job_dict taking precedence."""
    merged = (global_dict or {}).copy()
    merged.update(job_dict or {})
    return merged


def load_yaml_config(path):
    """Load a YAML config file. Raises OSError/yaml.YAMLError on failure — caller handles."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_job_path(job):
    """Resolve --job value: a path to a .yaml file, or a bare job name under config/jobs/."""
    if job.endswith(".yaml") and os.path.exists(job):
        return job
    candidate = os.path.join(JOBS_DIR, f"{job}.yaml")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(f"Job config not found: {job} (looked for {candidate})")


def build_config(job_path):
    """Load and merge global.yaml + job yaml into one effective config dict."""
    job_config = load_yaml_config(job_path)
    try:
        global_config = load_yaml_config(GLOBAL_CONFIG_PATH)
    except (OSError, yaml.YAMLError) as e:
        cli_logger.warning(f"Could not load global config: {e}")
        global_config = {}

    config = dict(job_config)
    config["retention"] = merge_dicts(global_config.get("retention"), job_config.get("retention"))
    config["restic_options"] = merge_dicts(global_config.get("restic_options"), job_config.get("restic_options"))

    exclude = list(job_config.get("exclude") or [])
    if global_config.get("use_common_exclude", True):
        common_exclude_path = os.path.join(CONFIG_DIR, "common_exclude.yaml")
        if os.path.exists(common_exclude_path):
            exclude = list(load_yaml_config(common_exclude_path) or []) + exclude
    config["exclude"] = exclude

    for key, value in global_config.items():
        if key in ("retention", "restic_options", "email"):
            continue
        if key not in config or config[key] is None:
            config[key] = value

    return config


def _restic_global_args_from_config(config):
    """Build restic's global (pre-subcommand) flags from restic_options."""
    opts = config.get("restic_options") or {}
    return restic_client.build_global_args(
        limit_upload=opts.get("limit_upload", 0),
        limit_download=opts.get("limit_download", 0),
        compression=opts.get("compression"),
        pack_size=opts.get("pack_size"),
        cache_dir=opts.get("cache_dir"),
        no_cache=opts.get("no_cache", False),
        retry_lock=opts.get("retry_lock"),
    )


def _restic_backup_options_from_config(config):
    """Build restic_client.backup()'s tuning kwargs from restic_options."""
    opts = config.get("restic_options") or {}
    return dict(
        extra_args=opts.get("extra_backup_args"),
        one_file_system=opts.get("one_file_system", False),
        exclude_caches=opts.get("exclude_caches", False),
        exclude_if_present=opts.get("exclude_if_present"),
        exclude_larger_than=opts.get("exclude_larger_than"),
        host=opts.get("host"),
        read_concurrency=opts.get("read_concurrency"),
    )


def run_job(job_path, do_init=False, do_prune=False, dry_run=False):
    """Run one snapshot backup job. Returns True on success, False on failure."""
    try:
        config = build_config(job_path)
    except (OSError, yaml.YAMLError) as e:
        cli_logger.error(f"Failed to load job config {job_path}: {e}")
        return False
    job_name = config.get("job_name", os.path.splitext(os.path.basename(job_path))[0])
    logger = setup_logger(job_name)

    repo_path = config.get("repo_path")
    if not repo_path:
        logger.error("No repo_path configured (set it in global.yaml or the job config)")
        return False

    password = os.environ.get("RESTIC_PASSWORD") or None
    password_file = os.environ.get("RESTIC_PASSWORD_FILE") or None
    if not password and not password_file:
        logger.error("Neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set in .env")
        return False

    paths = config.get("paths") or []
    if not paths:
        logger.error("No paths configured to back up")
        return False

    tags = config.get("tags") or []
    retention = config.get("retention") or {}

    os.makedirs(LOCK_DIR, exist_ok=True)
    job_name_sanitized = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
    lock_path = os.path.join(LOCK_DIR, f"{job_name_sanitized}.lock")

    try:
        lock_file = acquire_lock(lock_path)
    except RuntimeError as e:
        logger.error(f"Could not start backup: {e}")
        return False

    run_id = str(uuid.uuid4())
    # target_id/target_label must stay stable across every run of this job (not
    # per-run, e.g. run_id or a timestamp) so the dashboard groups all runs of
    # the same job together instead of giving each run its own row.
    target_id = job_name
    target_label = job_name
    start_time = time.time()
    global_args = _restic_global_args_from_config(config)

    try:
        send_backup_start(job_name, "snapshot", target_id, target_label,
                           run_id=run_id, dry_run=dry_run)
        logger.info(f"###### Starting backup: {job_name} ({socket.gethostname()}) ######")

        if not restic_client.repo_exists(repo_path, password, password_file, global_args=global_args):
            if not do_init:
                logger.error(
                    f"Repository does not exist: {repo_path}. Re-run with --init to create it."
                )
                duration = time.time() - start_time
                send_backup_complete(
                    job_name, "snapshot", target_id, target_label, duration,
                    run_id=run_id, success=False,
                    error_message=f"Repository does not exist: {repo_path}",
                    dry_run=dry_run,
                )
                return False
            logger.info(f"Initializing new restic repository: {repo_path}")
            send_backup_stage(job_name, "snapshot", target_id, target_label,
                               "Initializing repository", run_id=run_id, dry_run=dry_run)
            restic_client.init_repo(repo_path, password, password_file, global_args=global_args)

        send_backup_stage(job_name, "snapshot", target_id, target_label,
                           "Running backup", run_id=run_id, dry_run=dry_run)

        # Best-effort live progress: throttled independently for the dashboard
        # POST (wall-clock, so long jobs don't flood the API) and the local
        # log (10%-decile crossings, so long jobs don't flood the log file).
        progress_state = {"last_post": 0.0, "last_decile": -1}

        def on_progress(event):
            pct_done = event.get("percent_done")
            if pct_done is None:
                return
            pct = int(pct_done * 100)
            bytes_done = event.get("bytes_done")
            eta_seconds = event.get("seconds_remaining")
            decile = pct // 10
            if decile > progress_state["last_decile"]:
                progress_state["last_decile"] = decile
                eta_text = f", eta {eta_seconds}s" if eta_seconds is not None else ""
                logger.info(f"Progress: {pct}%{eta_text}")
            now = time.time()
            if now - progress_state["last_post"] >= 5:
                progress_state["last_post"] = now
                send_backup_progress(
                    job_name, "snapshot", target_id, run_id=run_id,
                    percent_complete=pct, eta_seconds=eta_seconds,
                    bytes_backed_up=bytes_done,
                )

        summary = restic_client.backup(
            repo_path, paths, tags=tags, exclude_patterns=config.get("exclude"),
            password=password, password_file=password_file, dry_run=dry_run,
            global_args=global_args, on_progress=on_progress,
            **_restic_backup_options_from_config(config),
        )

        duration = time.time() - start_time
        snapshot_id = summary.get("snapshot_id", "")
        # restic's own "short_id" is just the first 8 hex chars of the full id
        # (what `restic snapshots`/Restic Browser display), not a separate value.
        snapshot_short_id = snapshot_id[:8] if snapshot_id else ""
        files_backed_up = summary.get("files_new", 0) + summary.get("files_changed", 0)
        bytes_backed_up = summary.get("data_added", 0)

        logger.info(
            f"Backup complete: snapshot={snapshot_short_id} ({snapshot_id}) "
            f"files={files_backed_up} data_added={bytes_backed_up} duration={duration:.1f}s"
        )

        send_backup_complete(
            job_name, "snapshot", target_id, target_label,
            duration, run_id=run_id, files_backed_up=files_backed_up,
            bytes_backed_up=bytes_backed_up, success=True, dry_run=dry_run,
            snapshot_short_id=snapshot_short_id, snapshot_id=snapshot_id,
        )
        process_email_event(
            "backup_complete", f"JABS snapshot backup complete: {job_name}",
            f"{'[DRY RUN] ' if dry_run else ''}Backup of {job_name} completed in {duration:.1f}s "
            f"(snapshot {snapshot_short_id}, {files_backed_up} files changed).",
        )

        if config.get("verify_after_backup", True):
            try:
                logger.info("Running quick repository check (metadata only)")
                send_backup_stage(job_name, "snapshot", target_id, target_label,
                                   "Verifying repository (quick check)", run_id=run_id, dry_run=dry_run)
                restic_client.check(repo_path, password, password_file, global_args=global_args)
                logger.info("Quick repository check passed")
                send_check_result(job_name, "snapshot", target_id, target_label,
                                   run_id, success=True, dry_run=dry_run)
            except ResticError as e:
                # A quick-check failure doesn't invalidate the backup that was just
                # taken, but is serious enough to alert on unlike a prune failure.
                logger.error(f"Quick repository check FAILED: {e}")
                send_check_result(job_name, "snapshot", target_id, target_label,
                                   run_id, success=False, error_message=str(e), dry_run=dry_run)
                process_email_event(
                    "error", f"JABS snapshot repository check FAILED: {job_name}",
                    f"{'[DRY RUN] ' if dry_run else ''}Quick repository check after backup of "
                    f"{job_name} failed: {e}\nRepo: {repo_path}",
                )

        if do_prune or config.get("prune_after_backup"):
            try:
                logger.info(f"Running forget/prune for tags={tags}")
                send_backup_stage(job_name, "snapshot", target_id, target_label,
                                   "Pruning old snapshots", run_id=run_id, dry_run=dry_run)
                removed_ids = restic_client.forget_prune(
                    repo_path, tags=tags, password=password, password_file=password_file,
                    global_args=global_args, extra_args=retention.get("extra_forget_args"),
                    **{k: v for k, v in retention.items()
                       if k.startswith("keep_") or k == "group_by"},
                )
                if removed_ids:
                    logger.info(f"Pruned {len(removed_ids)} snapshot(s): {removed_ids}")
                    send_target_purged(target_id, removed_ids)
            except ResticError as e:
                # Prune failure doesn't invalidate a successful backup; log only.
                logger.warning(f"forget/prune failed: {e}")

        return True

    except ResticError as e:
        duration = time.time() - start_time
        logger.error(f"Backup failed: {e}")
        send_backup_complete(
            job_name, "snapshot", target_id, target_label, duration,
            run_id=run_id, success=False, error_message=str(e), dry_run=dry_run,
        )
        process_email_event(
            "error", f"JABS snapshot backup FAILED: {job_name}",
            f"{'[DRY RUN] ' if dry_run else ''}Backup of {job_name} failed after {duration:.1f}s: {e}",
        )
        return False

    finally:
        release_lock(lock_file)


def init_repo_only():
    """Initialize the shared restic repo directly from global.yaml, with no job involved."""
    try:
        global_config = load_yaml_config(GLOBAL_CONFIG_PATH)
    except (OSError, yaml.YAMLError) as e:
        print(f"ERROR: Could not load {GLOBAL_CONFIG_PATH}: {e}")
        sys.exit(2)

    repo_path = global_config.get("repo_path")
    if not repo_path:
        print(f"ERROR: No repo_path configured in {GLOBAL_CONFIG_PATH}")
        sys.exit(2)

    password = os.environ.get("RESTIC_PASSWORD") or None
    password_file = os.environ.get("RESTIC_PASSWORD_FILE") or None
    if not password and not password_file:
        print("ERROR: Neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set in .env")
        sys.exit(2)

    if restic_client.repo_exists(repo_path, password, password_file,
                                  global_args=_restic_global_args_from_config(global_config)):
        print(f"Repository already exists: {repo_path}")
        return

    print(f"Initializing new restic repository: {repo_path}")
    restic_client.init_repo(repo_path, password, password_file,
                             global_args=_restic_global_args_from_config(global_config))
    print("Repository initialized.")


def run_repo_check(read_data=False, read_data_subset=None):
    """Run `restic check` directly against the shared repo, with no job involved.

    Default is metadata-only (fast); pass read_data=True or read_data_subset
    for a slow, manual-only data read-back. Returns True/False.
    """
    try:
        global_config = load_yaml_config(GLOBAL_CONFIG_PATH)
    except (OSError, yaml.YAMLError) as e:
        print(f"ERROR: Could not load {GLOBAL_CONFIG_PATH}: {e}")
        return False

    repo_path = global_config.get("repo_path")
    if not repo_path:
        print(f"ERROR: No repo_path configured in {GLOBAL_CONFIG_PATH}")
        return False

    password = os.environ.get("RESTIC_PASSWORD") or None
    password_file = os.environ.get("RESTIC_PASSWORD_FILE") or None
    if not password and not password_file:
        print("ERROR: Neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set in .env")
        return False

    if read_data_subset:
        label = f"partial data check (subset={read_data_subset})"
    elif read_data:
        label = "full data check"
    else:
        label = "quick check (metadata only)"
    print(f"Running {label} against {repo_path} ...")

    try:
        restic_client.check(repo_path, password, password_file,
                             read_data=read_data, read_data_subset=read_data_subset,
                             global_args=_restic_global_args_from_config(global_config))
    except ResticError as e:
        print(f"Repository check FAILED: {e}")
        return False

    print("Repository check passed.")
    return True


def main():
    parser = argparse.ArgumentParser(description="JABS Snapshot Agent CLI")
    parser.add_argument("--job", help="Job name or path to job config YAML (omit to use --init alone)")
    parser.add_argument("--init", action="store_true", help="Initialize the repo if it doesn't exist")
    parser.add_argument("--prune", action="store_true", help="Run forget/prune after the backup")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run through to restic")
    parser.add_argument("--check", action="store_true",
                         help="Run a quick repo check (metadata only) directly; no --job needed")
    parser.add_argument("--full-check", action="store_true",
                         help="Run a deep repo check that reads all data (slow); no --job needed")
    parser.add_argument("--subset", metavar="PERCENT_OR_SIZE",
                         help="With --full-check, only read this subset of data (e.g. '5%%' or '500M')")
    args = parser.parse_args()

    if args.check or args.full_check:
        ok = run_repo_check(read_data=args.full_check, read_data_subset=args.subset if args.full_check else None)
        sys.exit(0 if ok else 1)

    if not args.job:
        if not args.init:
            parser.error("--job is required (or pass --init alone to just initialize the shared repo)")
        init_repo_only()
        sys.exit(0)

    try:
        job_path = resolve_job_path(args.job)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

    success = run_job(job_path, do_init=args.init, do_prune=args.prune, dry_run=args.dry_run)
    uptime_kuma.ping(
        "up" if success else "down",
        f"{'[DRY RUN] ' if args.dry_run else ''}{args.job}: {'OK' if success else 'FAILED'}",
        dry_run=args.dry_run,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
