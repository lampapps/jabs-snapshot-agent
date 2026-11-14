"""Finding and reading the Markdown files of an install (README, AGENTS and so on) for the console's Docs tab."""

import os
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

# Directories that hold dependencies, state or secrets, never documentation.
SKIP_DIRS = {"__pycache__", "node_modules", "site-packages", "vendor", "data", "logs", "keys", "venv", "env"}
MAX_FILES = 300
MAX_BYTES = 2 * 1024 * 1024
SUFFIXES = (".md", ".markdown")


class DocError(Exception):
    """A document that cannot be shown, with the reason."""


def find_markdown(root: Path) -> list:
    """Relative paths of the Markdown files under `root`: top-level files first (README first), then the rest."""
    root = Path(root)
    found = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.lower().endswith(SUFFIXES):
                found.append((Path(folder) / name).relative_to(root))
            if len(found) >= MAX_FILES:
                break
        if len(found) >= MAX_FILES:
            break

    def order(path: Path) -> tuple:
        return (len(path.parts) > 1, path.name.lower() != "readme.md", str(path).lower())

    return sorted(found, key=order)


def resolve_doc(root: Path, relative: object) -> Path:
    """The real path of a document under `root`; refuses anything outside it, non-Markdown or oversized."""
    root = Path(root).resolve()
    try:
        path = (root / relative).resolve()
        path.relative_to(root)
    except (ValueError, OSError) as exc:
        raise DocError(f"{relative} is outside {root}") from exc
    if not path.name.lower().endswith(SUFFIXES):
        raise DocError(f"{path.name} is not a Markdown file")
    if not path.is_file():
        raise DocError(f"{path} was not found")
    if path.stat().st_size > MAX_BYTES:
        raise DocError(f"{path.name} is larger than {MAX_BYTES // 1024 // 1024} MiB")
    return path


def read_doc(root: Path, relative: object) -> tuple:
    """(resolved path, text) of a document under `root`."""
    path = resolve_doc(root, relative)
    try:
        return path, path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocError(f"cannot read {path}: {exc}") from exc


def link_target(root: Path, current: Optional[Path], href: str) -> Optional[Path]:
    """The Markdown file a relative link points to (relative to the document being viewed), or None for web links,
    anchors and anything that is not a document inside the install."""
    parsed = urlparse(href)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    base = current.parent if current is not None else Path(root).resolve()
    try:
        return resolve_doc(root, (base / unquote(parsed.path)).relative_to(Path(root).resolve()))
    except (ValueError, DocError):
        return None
