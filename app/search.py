"""Search the repository for files by a partial, case-insensitive name (a `restic find` wrapper)."""

import sys
from typing import Optional

from . import restic

PAGE_SIZE = 30
GLOB_CHARS = set("*?[")


def build_pattern(query: str) -> str:
    """Wrap a plain query in wildcards for a contains search, unless it already has glob characters."""
    return query if any(c in GLOB_CHARS for c in query) else f"*{query}*"


def find_paths(repo_path: str, query: str, password: Optional[str], password_file: Optional[str],
               global_args: Optional[list] = None) -> list:
    """Matching file paths across all snapshots, without duplicates."""
    results = restic.find(repo_path, build_pattern(query), password=password, password_file=password_file,
                          ignore_case=True, global_args=global_args)
    seen: set = set()
    paths = []
    for entry in results:
        for match in entry.get("matches", []):
            path = match.get("path")
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def page_output(lines: list, page_size: int = PAGE_SIZE) -> None:
    """Print lines, pausing every page_size lines when a person is reading a long list."""
    if len(lines) <= page_size or not sys.stdout.isatty():
        print("\n".join(lines))
        return
    start = 0
    while start < len(lines):
        print("\n".join(lines[start:start + page_size]))
        start += page_size
        if start >= len(lines):
            break
        try:
            answer = input(f"-- {start}/{len(lines)} shown -- Enter for more, 'q' to quit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer in ("q", "quit"):
            return
