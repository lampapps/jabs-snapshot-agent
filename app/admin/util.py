"""Small helpers shared by the admin console (no Textual imports, so they are easy to test)."""

import time
from typing import Optional


def format_bytes(size: float) -> str:
    """'1.5 MiB' style text."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def ago(ts: Optional[float], now: Optional[float] = None) -> str:
    """'5 min ago' style text for an epoch time, or '-' when unknown."""
    if not ts:
        return "-"
    seconds = max(0, int((now if now is not None else time.time()) - ts))
    for limit, unit, size in ((90, "s", 1), (5400, "min", 60), (172800, "h", 3600)):
        if seconds < limit:
            return f"{seconds // size} {unit} ago"
    return f"{seconds // 86400} d ago"


def one_line(text: Optional[str], limit: int = 60) -> str:
    """Text collapsed to one line and cut to `limit` characters with an ellipsis; '-' when empty."""
    flat = " ".join((text or "").split())
    if not flat:
        return "-"
    return flat if len(flat) <= limit else flat[:limit - 1] + "\u2026"
