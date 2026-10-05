"""Push heartbeats to an Uptime Kuma push monitor. Mirrors the bash agents'
uptime_kuma_ping() (see nas_sync_agent/nas_sync.sh) so all JABS agents behave
the same way: best-effort, never raises, silently disabled when unconfigured.
"""

import logging
from urllib.parse import quote

import requests

from settings import UPTIME_KUMA_URL

logger = logging.getLogger("uptime_kuma")


def ping(status: str, message: str = "", dry_run: bool = False) -> bool:
    """Send an 'up' or 'down' heartbeat. No-op if unconfigured or dry_run."""
    if not UPTIME_KUMA_URL:
        return False
    if dry_run:  # never ping Uptime Kuma during dry runs
        return False

    base_url = UPTIME_KUMA_URL.split("?", 1)[0]  # drop any query string from Kuma's UI
    url = f"{base_url}?status={status}&msg={quote(message)}&ping="

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Uptime Kuma ping failed: {response.status_code} {response.text}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"Uptime Kuma ping failed: {e}")
        return False
