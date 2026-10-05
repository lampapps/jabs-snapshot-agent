"""Client for reporting agent events and metrics to the central JABS dashboard."""

import logging
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

from settings import ENV_PATH, VERSION, AGENT_TYPE, AGENT_KEY

logger = logging.getLogger("monitoring")

load_dotenv(ENV_PATH, override=True)  # .env must win over inherited shell/cron env vars

# JABS_DASHBOARD_URL is the current name; JABS_SERVER_URL still works as a
# deprecated alias for .env files written before the Dashboard rename.
DASHBOARD_URL = os.getenv("JABS_DASHBOARD_URL") or os.getenv("JABS_SERVER_URL")


def _auth_headers() -> dict:
    """Headers required to authenticate to the dashboard API."""
    if not AGENT_KEY:
        logger.warning("JABS_AGENT_KEY is not set; dashboard requests will be rejected")
    return {"X-API-Key": AGENT_KEY or ""}


def _dashboard_configured() -> bool:
    """Check that JABS_DASHBOARD_URL is set before attempting a request."""
    if not DASHBOARD_URL:
        logger.warning("JABS_DASHBOARD_URL is not set; skipping dashboard request")
        return False
    return True


def send_event(
    event_type: str,
    message: str,
    run_id: str = None,
    target_id: str = None,
    job_name: str = None,
    backup_type: str = None,
    target_label: str = None,
    stage: str = None,
    status: str = None,
    duration_seconds: float = None,
    files_backed_up: int = None,
    bytes_backed_up: int = None,
    bytes_compressed: int = None,
    percent_complete: int = None,
    bytes_per_second: float = None,
    eta_seconds: float = None,
    current_item: str = None,
    error_code: Optional[int] = None,
    error_message: str = None,
    timestamp: Optional[int] = None,
    cron_schedule: str = None,
    external_id: str = None,
) -> bool:
    """Send an event to the dashboard. Never raises; returns True/False."""
    if not _dashboard_configured():
        return False
    try:
        payload = {
            "version": VERSION,
            "agent_type": AGENT_TYPE,
            "event_type": event_type,
            "message": message,
            "timestamp": timestamp or int(time.time()),
        }

        if run_id is not None:
            payload["run_id"] = run_id
        if target_id is not None:
            payload["target_id"] = target_id
        if job_name is not None:
            payload["job_name"] = job_name
        if backup_type is not None:
            payload["backup_type"] = backup_type
        if target_label is not None:
            payload["target_label"] = target_label
        if stage is not None:
            payload["stage"] = stage
        if status is not None:
            payload["status"] = status
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if files_backed_up is not None:
            payload["files_backed_up"] = files_backed_up
        if bytes_backed_up is not None:
            payload["bytes_backed_up"] = bytes_backed_up
        if bytes_compressed is not None:
            payload["bytes_compressed"] = bytes_compressed
        if percent_complete is not None:
            payload["percent_complete"] = percent_complete
        if bytes_per_second is not None:
            payload["bytes_per_second"] = bytes_per_second
        if eta_seconds is not None:
            payload["eta_seconds"] = eta_seconds
        if current_item is not None:
            payload["current_item"] = current_item
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            payload["error_message"] = error_message
        if cron_schedule is not None:
            payload["cron_schedule"] = cron_schedule
        if external_id is not None:
            payload["external_id"] = external_id

        logger.debug(f"Sending event to dashboard: {event_type} - {message}")

        response = requests.post(
            f"{DASHBOARD_URL}/api/monitoring/events",
            json=payload,
            headers=_auth_headers(),
            timeout=5,
        )

        if response.status_code in (200, 201):
            logger.debug("Event sent successfully")
            return True

        logger.warning(f"Failed to send event: {response.status_code} {response.text}")
        return False

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to send event: {e}")
        return False


def _tag(message: str, dry_run: bool) -> str:
    """Prefix a message with [DRY RUN] when dry_run is True."""
    return f"[DRY RUN] {message}" if dry_run else message


def send_backup_start(job_name: str, backup_type: str, target_id: str,
                       target_label: str, run_id: str = None, dry_run: bool = False) -> bool:
    return send_event(
        event_type="heartbeat",
        message=_tag(f"Starting {backup_type} backup for {job_name}", dry_run),
        run_id=run_id,
        target_id=target_id,
        job_name=job_name,
        backup_type=backup_type,
        target_label=target_label,
        stage="Starting backup",
    )


def send_backup_stage(job_name: str, backup_type: str, target_id: str,
                       target_label: str, stage: str, run_id: str = None,
                       dry_run: bool = False) -> bool:
    return send_event(
        event_type="heartbeat",
        message=_tag(f"Backup {job_name}: {stage}", dry_run),
        run_id=run_id,
        target_id=target_id,
        job_name=job_name,
        backup_type=backup_type,
        target_label=target_label,
        stage=stage,
    )


def send_backup_progress(job_name: str, backup_type: str, target_id: str,
                          run_id: str = None, percent_complete: int = None,
                          bytes_per_second: float = None, eta_seconds: float = None,
                          current_item: str = None, files_backed_up: int = None,
                          bytes_backed_up: int = None) -> bool:
    """Best-effort mid-run progress update. No duration_seconds, so this never
    finalizes the job — the dashboard applies it as a non-finalizing update."""
    return send_event(
        event_type="heartbeat",
        message="Backup in progress",
        run_id=run_id,
        target_id=target_id,
        job_name=job_name,
        backup_type=backup_type,
        percent_complete=percent_complete,
        bytes_per_second=bytes_per_second,
        eta_seconds=eta_seconds,
        current_item=current_item,
        files_backed_up=files_backed_up,
        bytes_backed_up=bytes_backed_up,
    )


def send_backup_complete(job_name: str, backup_type: str, target_id: str,
                          target_label: str, duration_seconds: float,
                          run_id: str = None, files_backed_up: int = 0,
                          bytes_backed_up: int = 0, success: bool = True,
                          error_message: Optional[str] = None, dry_run: bool = False,
                          snapshot_short_id: Optional[str] = None,
                          snapshot_id: Optional[str] = None) -> bool:
    if success:
        message = f"Backup complete (snapshot {snapshot_short_id})" if snapshot_short_id else "Backup complete"
        return send_event(
            event_type="backup_complete",
            message=_tag(message, dry_run),
            run_id=run_id,
            target_id=target_id,
            job_name=job_name,
            backup_type=backup_type,
            target_label=target_label,
            stage="Completed",
            status="success",
            duration_seconds=duration_seconds,
            files_backed_up=files_backed_up,
            bytes_backed_up=bytes_backed_up,
            external_id=snapshot_id,
        )

    return send_event(
        event_type="error",
        message=_tag("Backup failed", dry_run),
        run_id=run_id,
        target_id=target_id,
        job_name=job_name,
        backup_type=backup_type,
        target_label=target_label,
        stage="Error",
        status="failed",
        duration_seconds=duration_seconds,
        error_message=error_message,
    )


def send_scheduler_check(running_jobs: int = 0) -> bool:
    """Bare heartbeat sent by scheduler.py each time it checks for due jobs."""
    return send_event(
        event_type="heartbeat",
        message=f"Scheduler check completed. {running_jobs} job(s) triggered.",
        stage="Scheduler check",
    )


def send_job_schedule(job_name: str, cron_schedule: str) -> bool:
    """Bare heartbeat reporting job_name's configured cron schedule(s), so the
    dashboard can compute its "Next Event" column. Sent on every scheduler
    check regardless of whether the job is due."""
    return send_event(
        event_type="heartbeat",
        message=f"{job_name} schedule: {cron_schedule}",
        job_name=job_name,
        cron_schedule=cron_schedule,
    )


def send_target_purged(target_id: str, external_ids: list, message: str = None) -> bool:
    """Report that restic `forget --prune` removed the given snapshot IDs for
    target_id. Never raises; returns True/False. No-op (returns False) if
    external_ids is empty."""
    if not external_ids:
        return False
    if not _dashboard_configured():
        return False
    try:
        payload = {
            "target_id": target_id,
            "external_ids": external_ids,
            "message": message or f"restic forget --prune removed {len(external_ids)} snapshot(s)",
            "timestamp": int(time.time()),
        }
        response = requests.post(
            f"{DASHBOARD_URL}/api/monitoring/target-purged",
            json=payload,
            headers=_auth_headers(),
            timeout=5,
        )
        if response.status_code == 200:
            logger.debug("Target-purged reported successfully")
            return True
        logger.warning(f"Failed to report target-purged: {response.status_code} {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to report target-purged: {e}")
        return False


def send_check_result(job_name: str, backup_type: str, target_id: str,
                       target_label: str, run_id: str, success: bool,
                       error_message: str = None, dry_run: bool = False) -> bool:
    """Report the outcome of the quick `restic check` run after a backup, as a
    heartbeat (pass) or error (fail) event."""
    stage = "Repository check (quick)"
    if success:
        return send_event(
            event_type="heartbeat",
            message=_tag(f"{stage} passed", dry_run),
            run_id=run_id,
            target_id=target_id,
            job_name=job_name,
            backup_type=backup_type,
            target_label=target_label,
            stage=stage,
            status="success",
        )
    return send_event(
        event_type="error",
        message=_tag(f"{stage} FAILED", dry_run),
        run_id=run_id,
        target_id=target_id,
        job_name=job_name,
        backup_type=backup_type,
        target_label=target_label,
        stage=stage,
        status="failed",
        error_message=error_message,
    )
