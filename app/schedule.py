"""Which jobs are due. Schedules are 5-field cron expressions evaluated here with croniter; no cron daemon.

For each job and expression the state remembers the latest scheduled slot already served. A slot
newer than that is due, so a run missed while the machine was off (or another backup was still
running) happens once at the next tick. A job seen for the first time only gets a baseline.
"""

from datetime import datetime, timedelta
from typing import Optional

from croniter import croniter

from . import state


def local_now() -> datetime:
    return datetime.now().astimezone()


def latest_slot(cron: str, now: datetime) -> datetime:
    """The most recent scheduled time at or before `now`."""
    return croniter(cron, now + timedelta(seconds=1)).get_prev(datetime)


def next_slot(cron: str, now: datetime) -> datetime:
    return croniter(cron, now).get_next(datetime)


def due_jobs(jobs: list, data: dict, now: datetime) -> list:
    """Jobs with an unserved slot; marks those slots served in `data` (call inside a state transaction)."""
    due = []
    for job in jobs:
        served = state.job_entry(data, job.name).setdefault("served", {})
        job_due = False
        for cron in job.enabled_crons():
            slot = latest_slot(cron, now).timestamp()
            previous = served.get(cron)
            served[cron] = slot
            if previous is not None and slot > previous:
                job_due = True
        if job_due:
            due.append(job)
    return due


def next_run(job, now: datetime) -> Optional[datetime]:
    """The soonest upcoming run across the job's enabled schedules."""
    crons = job.enabled_crons()
    return min((next_slot(cron, now) for cron in crons), default=None)
