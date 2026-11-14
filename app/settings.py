"""Paths, constants and environment access. Tunables live in config/config.yaml, secrets in .env."""

import os
import socket
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

VERSION = "0.3.0"
AGENT_TYPE = "Snapshot"

# JABS_HOME relocates everything the agent owns (used by tests and side-by-side installs).
BASE_DIR = Path(os.environ.get("JABS_HOME") or Path(__file__).resolve().parent.parent)
CONFIG_DIR = BASE_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
LEGACY_CONFIG_PATH = CONFIG_DIR / "global.yaml"
JOBS_DIR = CONFIG_DIR / "jobs"
COMMON_EXCLUDE_PATH = CONFIG_DIR / "common_exclude.yaml"
DATA_DIR = BASE_DIR / "data"
LOCK_DIR = DATA_DIR / "locks"
STATE_PATH = DATA_DIR / "state.json"
ENV_PATH = BASE_DIR / ".env"

# A lock older than this is treated as left behind by a crashed run.
STALE_LOCK_S = 7200
DASHBOARD_TIMEOUT_S = 5


def load_env() -> None:
    """Load .env without overriding variables already set in the environment."""
    load_dotenv(ENV_PATH)


def env_mode() -> str:
    """'development' or 'production' (default), from ENV_MODE."""
    return os.environ.get("ENV_MODE", "production")


def restic_bin() -> str:
    return os.environ.get("RESTIC_BIN") or "restic"


def restic_password() -> tuple:
    """(password, password_file) from the environment; either may be None."""
    return os.environ.get("RESTIC_PASSWORD") or None, os.environ.get("RESTIC_PASSWORD_FILE") or None


def dashboard_url() -> str:
    return (os.environ.get("JABS_DASHBOARD_URL") or "").strip().rstrip("/")


def agent_key() -> str:
    return (os.environ.get("JABS_AGENT_KEY") or "").strip()


def uptime_kuma_url() -> str:
    return (os.environ.get("UPTIME_KUMA_URL") or "").strip()


def machine_hostname(override: Optional[str] = None) -> str:
    """The name this agent reports for its machine: the configured override, else the system hostname."""
    return override or socket.gethostname()
