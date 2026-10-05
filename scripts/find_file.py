#!/usr/bin/env python3
"""JABS Snapshot Find — search for files across all snapshots in the repo.

Searches by partial, case-insensitive filename/path match (not restic's raw
glob syntax) and prints a deduplicated list of matching paths, paginated if
long. Read-only; never modifies the repository.

Usage:
    python scripts/find_file.py [query]

If query is omitted, you'll be prompted for one.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

import restic_client
from restic_client import ResticError
from settings import ENV_PATH, GLOBAL_CONFIG_PATH

load_dotenv(ENV_PATH, override=True)

PAGE_SIZE = 30
GLOB_CHARS = set("*?[")


def load_repo_path():
    """Read repo_path from config/global.yaml (one repo per machine)."""
    with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    repo_path = config.get("repo_path")
    if not repo_path:
        print(f"No repo_path configured in {GLOBAL_CONFIG_PATH}")
        sys.exit(1)
    return repo_path


def get_passphrase():
    password = os.environ.get("RESTIC_PASSWORD") or None
    password_file = os.environ.get("RESTIC_PASSWORD_FILE") or None
    if not password and not password_file:
        print("RESTIC_PASSWORD / RESTIC_PASSWORD_FILE not set in environment (.env).")
        sys.exit(1)
    return password, password_file


def build_pattern(query):
    """Wrap a plain query in glob wildcards for a "contains" search, unless
    the user already typed their own glob metacharacters."""
    if any(c in GLOB_CHARS for c in query):
        return query
    return f"*{query}*"


def find_paths(repo_path, query, password, password_file):
    """Return a deduplicated list of matching file paths across all snapshots."""
    pattern = build_pattern(query)
    try:
        results = restic_client.find(
            repo_path, pattern, password=password, password_file=password_file, ignore_case=True
        )
    except ResticError as e:
        print(f"Search failed: {e}")
        sys.exit(1)

    seen = set()
    paths = []
    for entry in results:
        for match in entry.get("matches", []):
            path = match.get("path")
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def page_output(lines, page_size=PAGE_SIZE):
    """Print lines, pausing every page_size lines if the list is long."""
    total = len(lines)
    if total <= page_size:
        for line in lines:
            print(line)
        return

    start = 0
    while start < total:
        for line in lines[start:start + page_size]:
            print(line)
        start += page_size
        if start >= total:
            break
        try:
            response = input(f"-- {start}/{total} shown -- Enter for more, 'q' to quit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if response in ("q", "quit"):
            return


def main():
    parser = argparse.ArgumentParser(
        description="Search for files by partial, case-insensitive match across all snapshots in the repo."
    )
    parser.add_argument("query", nargs="?", help="Filename or path fragment to search for")
    args = parser.parse_args()

    query = args.query
    if not query:
        try:
            query = input("Enter a filename or path fragment to search for: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
    if not query:
        print("No search term given.")
        return

    repo_path = load_repo_path()
    password, password_file = get_passphrase()

    paths = find_paths(repo_path, query, password, password_file)
    if not paths:
        print(f"No matches found for '{query}'.")
        return

    print(f"{len(paths)} match(es) for '{query}':\n")
    page_output(paths)


if __name__ == "__main__":
    main()
