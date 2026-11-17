"""The only place that talks to the JABS dashboard. Every method is a no-op returning False in standalone mode."""

import logging
import re
import time
from typing import Optional

import requests

from . import settings
from .config import Config

logger = logging.getLogger(__name__)

# After a connection failure, skip the dashboard this long so a down dashboard cannot stall a backup.
COOLDOWN_S = 30

# restic accepts scheme://user:password@host repository URLs; the password must never reach the dashboard.
_URL_PASSWORD = re.compile(r"(://[^/@:\s]*):[^/@\s]*@")


def redact_repo(repo_path: str) -> str:
    return _URL_PASSWORD.sub(r"\1@", repo_path)


def jobs_payload(jobs: list) -> list:
    """What /api/v2/jobs takes: each job's paths as its source and its repository as its destination."""
    return [{"job_name": job.name, "targets": [{"name": job.name, "source": "\n".join(job.paths),
                                               "destination": redact_repo(job.repo_path)}]} for job in jobs]
COOLDOWN_S = 30


class Client:
    def __init__(self, url: str = "", key: str = "", hostname: str = ""):
        self.url = url.rstrip("/")
        self.key = key
        self.hostname = hostname
        self._cooldown_until = 0.0

    @classmethod
    def from_env(cls, cfg: Config) -> "Client":
        """A client for the configured dashboard, or a disabled one when running standalone."""
        hostname = settings.machine_hostname(cfg.hostname)
        url, key = settings.dashboard_url(), settings.agent_key()
        if not cfg.dashboard.enabled or not (url and key):
            return cls(hostname=hostname)
        return cls(url, key, hostname)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def describe(self) -> str:
        if self.enabled:
            return f"reporting to {self.url} as host '{self.hostname}'"
        return "standalone (no dashboard)"

    def _post(self, path: str, payload: dict) -> Optional[requests.Response]:
        """POST JSON to the dashboard; None when disabled, cooling down or unreachable."""
        if not self.enabled or time.monotonic() < self._cooldown_until:
            return None
        try:
            return requests.post(f"{self.url}{path}", json=payload, headers={"X-API-Key": self.key},
                                 timeout=settings.DASHBOARD_TIMEOUT_S)
        except requests.exceptions.RequestException as exc:
            self._cooldown_until = time.monotonic() + COOLDOWN_S
            logger.debug("dashboard request %s failed: %s", path, exc)
            return None

    def _post_ok(self, path: str, payload: dict, ok: tuple = (200, 201), quiet: tuple = ()) -> bool:
        response = self._post(path, payload)
        if response is None:
            return False
        if response.status_code in ok:
            return True
        level = logging.DEBUG if response.status_code in quiet else logging.WARNING
        logger.log(level, "dashboard rejected %s: %s %s", path, response.status_code, response.text[:200])
        return False

    # ------------------------------------------------------------ events

    def send_event(self, event_type: str, message: str, run_id=None, target_id=None, job_name=None,
                   backup_type=None, target_label=None, stage=None, status=None, duration_seconds=None,
                   files_backed_up=None, bytes_backed_up=None, bytes_compressed=None, percent_complete=None,
                   bytes_per_second=None, eta_seconds=None, current_item=None, error_code=None,
                   error_message=None, timestamp=None, cron_schedule=None, external_id=None) -> bool:
        if not self.enabled:
            return False
        payload = {
            "version": settings.VERSION, "agent_type": settings.AGENT_TYPE, "hostname": self.hostname,
            "event_type": event_type, "message": message, "timestamp": timestamp or int(time.time()),
        }
        optional = {
            "run_id": run_id, "target_id": target_id, "job_name": job_name, "backup_type": backup_type,
            "target_label": target_label, "stage": stage, "status": status, "duration_seconds": duration_seconds,
            "files_backed_up": files_backed_up, "bytes_backed_up": bytes_backed_up,
            "bytes_compressed": bytes_compressed, "percent_complete": percent_complete,
            "bytes_per_second": bytes_per_second, "eta_seconds": eta_seconds, "current_item": current_item,
            "error_code": error_code, "error_message": error_message, "cron_schedule": cron_schedule,
            "external_id": external_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return self._post_ok("/api/monitoring/events", payload)

    @staticmethod
    def _tag(message: str, dry_run: bool) -> str:
        return f"[DRY RUN] {message}" if dry_run else message

    def send_backup_start(self, job_name, backup_type, target_id, target_label, run_id=None, dry_run=False) -> bool:
        return self.send_event(
            "heartbeat", self._tag(f"Starting {backup_type} backup for {job_name}", dry_run), run_id=run_id,
            target_id=target_id, job_name=job_name, backup_type=backup_type, target_label=target_label,
            stage="Starting backup")

    def send_backup_stage(self, job_name, backup_type, target_id, target_label, stage, run_id=None,
                          dry_run=False) -> bool:
        return self.send_event(
            "heartbeat", self._tag(f"Backup {job_name}: {stage}", dry_run), run_id=run_id, target_id=target_id,
            job_name=job_name, backup_type=backup_type, target_label=target_label, stage=stage)

    def send_backup_progress(self, job_name, backup_type, target_id, run_id=None, percent_complete=None,
                             bytes_per_second=None, eta_seconds=None, current_item=None, files_backed_up=None,
                             bytes_backed_up=None) -> bool:
        """Mid-run progress; it has no duration, so the dashboard never treats it as the end of the run."""
        return self.send_event(
            "heartbeat", "Backup in progress", run_id=run_id, target_id=target_id, job_name=job_name,
            backup_type=backup_type, percent_complete=percent_complete, bytes_per_second=bytes_per_second,
            eta_seconds=eta_seconds, current_item=current_item, files_backed_up=files_backed_up,
            bytes_backed_up=bytes_backed_up)

    def send_backup_complete(self, job_name, backup_type, target_id, target_label, duration_seconds, run_id=None,
                             files_backed_up=0, bytes_backed_up=0, success=True, error_message=None,
                             dry_run=False, snapshot_short_id=None, snapshot_id=None) -> bool:
        if success:
            message = f"Backup complete (snapshot {snapshot_short_id})" if snapshot_short_id else "Backup complete"
            return self.send_event(
                "backup_complete", self._tag(message, dry_run), run_id=run_id, target_id=target_id,
                job_name=job_name, backup_type=backup_type, target_label=target_label, stage="Completed",
                status="success", duration_seconds=duration_seconds, files_backed_up=files_backed_up,
                bytes_backed_up=bytes_backed_up, external_id=snapshot_id)
        return self.send_event(
            "error", self._tag("Backup failed", dry_run), run_id=run_id, target_id=target_id, job_name=job_name,
            backup_type=backup_type, target_label=target_label, stage="Error", status="failed",
            duration_seconds=duration_seconds, error_message=error_message)

    def send_check_result(self, job_name, backup_type, target_id, target_label, run_id, success,
                          error_message=None, dry_run=False) -> bool:
        stage = "Repository check (quick)"
        if success:
            return self.send_event(
                "heartbeat", self._tag(f"{stage} passed", dry_run), run_id=run_id, target_id=target_id,
                job_name=job_name, backup_type=backup_type, target_label=target_label, stage=stage,
                status="success")
        return self.send_event(
            "error", self._tag(f"{stage} FAILED", dry_run), run_id=run_id, target_id=target_id, job_name=job_name,
            backup_type=backup_type, target_label=target_label, stage=stage, status="failed",
            error_message=error_message)

    def send_scheduler_check(self, due_jobs: int = 0) -> bool:
        """Bare heartbeat from each tick: the agent is alive."""
        return self.send_event("heartbeat", f"Scheduler check: {due_jobs} job(s) due", stage="Scheduler check")

    def send_job_schedule(self, job_name: str, cron_schedule: str) -> bool:
        """Bare heartbeat carrying a job's cron schedule(s), which the dashboard uses for "next run"."""
        return self.send_event("heartbeat", f"{job_name} schedule: {cron_schedule}", job_name=job_name,
                               cron_schedule=cron_schedule)

    def send_target_purged(self, target_id: str, external_ids: list, message: Optional[str] = None) -> bool:
        """Tell the dashboard which snapshots `restic forget --prune` removed for a job."""
        if not external_ids:
            return False
        return self._post_ok("/api/monitoring/target-purged", {
            "target_id": target_id, "external_ids": external_ids, "timestamp": int(time.time()),
            "message": message or f"restic forget --prune removed {len(external_ids)} snapshot(s)",
        }, ok=(200,))

    # ------------------------------------------------------------ jobs

    def send_jobs(self, jobs: list) -> bool:
        """Every configured job with its source and destination, for the Agents page; needs dashboard 0.3.0+."""
        return self._post_ok("/api/v2/jobs", {"hostname": self.hostname, "jobs": jobs}, quiet=(404,))

    # ------------------------------------------------------------ storage

    def post_storage(self, samples: list) -> bool:
        if not samples:
            return False
        return self._post_ok("/api/v2/storage", {"hostname": self.hostname, "samples": samples})
