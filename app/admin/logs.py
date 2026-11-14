"""Logging for the console: keep records off the terminal the TUI owns, and leave an audit trail in the journal."""

import collections
import getpass
import logging
import logging.handlers
import os

AUDIT_LOGGER = "jabs.admin"


class Capture(logging.Handler):
    """Collects formatted records so the console can show them instead of letting them corrupt the screen."""

    def __init__(self, limit: int = 500):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.lines: collections.deque = collections.deque(maxlen=limit)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def drain(self) -> list:
        lines = list(self.lines)
        self.lines.clear()
        return lines


def install() -> Capture:
    """Replace the root logger's stream handlers with a Capture and add a syslog audit handler when available."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
    capture = Capture()
    root.addHandler(capture)
    if os.path.exists("/dev/log"):
        try:
            syslog = logging.handlers.SysLogHandler(address="/dev/log")
        except OSError:
            return capture
        syslog.setFormatter(logging.Formatter("jabs-admin: %(message)s"))
        syslog.setLevel(logging.INFO)
        audit_logger = logging.getLogger(AUDIT_LOGGER)
        audit_logger.addHandler(syslog)
    return capture


def audit(action: str, **details: object) -> None:
    """Record an administrative change; never pass keys, hashes or passwords."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "?"
    parts = " ".join(f"{key}={value}" for key, value in details.items())
    logging.getLogger(AUDIT_LOGGER).info("%s by %s %s", action, user, parts)
