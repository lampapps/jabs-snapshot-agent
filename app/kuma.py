"""Heartbeats to an Uptime Kuma push monitor: best effort, never raises, off unless UPTIME_KUMA_URL is set."""

import logging
from urllib.parse import quote

import requests

from . import settings

logger = logging.getLogger(__name__)

TIMEOUT_S = 10


def ping(status: str, message: str = "", dry_run: bool = False) -> bool:
    """Send an 'up' or 'down' heartbeat; nothing happens when unconfigured or on a dry run."""
    url = settings.uptime_kuma_url()
    if not url or dry_run:
        return False
    # Kuma's UI shows the URL with a query string of its own; drop it.
    target = f"{url.split('?', 1)[0]}?status={status}&msg={quote(message)}&ping="
    try:
        response = requests.get(target, timeout=TIMEOUT_S)
    except requests.exceptions.RequestException as exc:
        logger.warning("Uptime Kuma ping failed: %s", exc)
        return False
    if response.status_code != 200:
        logger.warning("Uptime Kuma ping failed: %s %s", response.status_code, response.text)
        return False
    return True
