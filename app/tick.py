"""Periodic work, run by the systemd timer (`python -m app tick`): heartbeat, due jobs, storage report."""

import logging
from datetime import datetime
from typing import Optional

from . import config, runner, schedule, state, storage
from .config import Config
from .dashboard import Client

logger = logging.getLogger(__name__)


class Tick:
    def __init__(self, cfg: Config, client: Client, now: datetime):
        self.cfg, self.client, self.now = cfg, client, now
        self.jobs: list = []
        self.due: list = []

    def load(self) -> None:
        self.jobs, errors = config.load_jobs(self.cfg)
        for message in errors:
            logger.error("skipping a job file: %s", message)
        with state.transaction() as data:
            self.due = schedule.due_jobs(self.jobs, data, self.now)

    def heartbeat(self) -> None:
        self.client.send_scheduler_check(len(self.due))
        for job in self.jobs:
            crons = job.enabled_crons()
            if crons:
                self.client.send_job_schedule(job.name, ",".join(crons))

    def run_due_jobs(self) -> None:
        for job in self.due:
            logger.info("Job '%s' is due", job.name)
            try:
                runner.run_job(job, self.cfg, self.client)
            except Exception:  # pylint: disable=broad-except  # one failing job must not stop the next one
                logger.exception("Job '%s' crashed", job.name)

    def report_storage(self) -> None:
        if not (self.client.enabled and self.cfg.storage.report):
            return
        repo_paths = sorted({job.repo_path for job in self.jobs} or ({self.cfg.repo_path} if self.cfg.repo_path else set()))
        self.client.post_storage(storage.collect_scheduled(self.cfg, repo_paths))


# Heartbeat first so a long backup does not keep the dashboard waiting; storage last.
TASKS = [("heartbeat", Tick.heartbeat), ("due jobs", Tick.run_due_jobs), ("storage", Tick.report_storage)]


def run_once(cfg: Config, client: Client, now: Optional[datetime] = None) -> None:
    """Plan what is due, then run every task once. A failing task is logged and does not stop the others."""
    tick = Tick(cfg, client, now or schedule.local_now())
    try:
        tick.load()
    except Exception:  # pylint: disable=broad-except  # a bad state file must not stop the heartbeat
        logger.exception("could not plan due jobs")
    for name, task in TASKS:
        try:
            task(tick)
        except Exception:  # pylint: disable=broad-except  # tasks are independent
            logger.exception("tick task '%s' failed", name)
    logger.info("tick complete: %d job(s) due", len(tick.due))
