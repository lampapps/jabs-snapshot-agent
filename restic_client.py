"""Thin subprocess wrapper around the restic CLI.

All functions shell out to the `restic` binary (see settings.RESTIC_BIN) with
the repository/password passed via environment variables, never as CLI args
(so they never appear in `ps` output or logs). Any non-zero exit raises
ResticError with restic's stderr attached; callers decide how to handle it —
this module never logs or swallows failures itself.
"""

import json
import os
import subprocess

from settings import RESTIC_BIN


class ResticError(RuntimeError):
    """Raised when a restic invocation exits non-zero."""


def build_global_args(limit_upload=0, limit_download=0, compression=None,
                       pack_size=None, cache_dir=None, no_cache=False,
                       retry_lock=None):
    """
    Build restic's global flags (valid before any subcommand, e.g.
    `restic --limit-upload 1000 backup ...`).

    limit_upload / limit_download: KiB/s, 0 = unlimited.
    compression: "auto" | "max" | "off" (repo must be format v2).
    pack_size: target pack file size in MiB.
    cache_dir: override restic's local metadata cache location.
    no_cache: disable the local cache entirely.
    retry_lock: duration (e.g. "5m") to wait for a repo lock instead of
        failing immediately — useful when jobs/agents share a repo.
    """
    args = []
    if limit_upload:
        args += ["--limit-upload", str(limit_upload)]
    if limit_download:
        args += ["--limit-download", str(limit_download)]
    if compression:
        args += ["--compression", compression]
    if pack_size:
        args += ["--pack-size", str(pack_size)]
    if no_cache:
        args.append("--no-cache")
    elif cache_dir:
        args += ["--cache-dir", cache_dir]
    if retry_lock:
        args += ["--retry-lock", retry_lock]
    return args


def _repo_env(repo_path, password=None, password_file=None):
    """Build the subprocess environment for a restic invocation against repo_path."""
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = repo_path
    # Only one of these should be set; prefer the file-based one if both are present.
    if password_file:
        env["RESTIC_PASSWORD_FILE"] = password_file
        env.pop("RESTIC_PASSWORD", None)
    elif password:
        env["RESTIC_PASSWORD"] = password
        env.pop("RESTIC_PASSWORD_FILE", None)
    return env


def run_restic(args, repo_path, password=None, password_file=None,
               capture_json=False, global_args=None):
    """
    Run `restic <args>` against repo_path and return its stdout.

    Args:
        args: list of CLI arguments (not including the `restic` binary itself).
        repo_path: value for RESTIC_REPOSITORY.
        password / password_file: exactly one should be set; passed as
            RESTIC_PASSWORD / RESTIC_PASSWORD_FILE env vars.
        capture_json: if True, parses stdout as JSON (or newline-delimited JSON
            objects, returned as a list) and returns the parsed value instead
            of the raw string.
        global_args: restic flags that must precede the subcommand (see
            build_global_args), e.g. --limit-upload, --compression.

    Raises:
        ResticError: if restic exits non-zero.
    """
    env = _repo_env(repo_path, password, password_file)
    cmd = [RESTIC_BIN] + list(global_args or []) + list(args)
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise ResticError(
            f"restic {' '.join(args)} failed (exit {e.returncode}): {e.stderr.strip()}"
        ) from e
    except FileNotFoundError as e:
        raise ResticError(f"restic binary not found: {RESTIC_BIN}") from e

    if not capture_json:
        return result.stdout

    return parse_json_output(result.stdout)


def parse_json_output(stdout):
    """Parse restic --json output, which may be a single JSON value or
    newline-delimited JSON objects (as emitted by `restic backup --json`)."""
    stdout = stdout.strip()
    if not stdout:
        return None

    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) == 1:
        return json.loads(lines[0])
    return [json.loads(line) for line in lines]


def repo_exists(repo_path, password=None, password_file=None, global_args=None):
    """Return True if a restic repository already exists at repo_path."""
    try:
        run_restic(["cat", "config"], repo_path, password, password_file, global_args=global_args)
        return True
    except ResticError:
        return False


def init_repo(repo_path, password=None, password_file=None, global_args=None):
    """Initialize a new restic repository at repo_path."""
    run_restic(["init"], repo_path, password, password_file, global_args=global_args)


def _run_restic_backup_streaming(args, repo_path, password=None, password_file=None,
                                  global_args=None, on_progress=None):
    """Like run_restic(..., capture_json=True), but streams stdout line-by-line
    so on_progress(dict) can be called for each `message_type":"status"` line
    restic emits while the backup is running. Returns the list of parsed JSON
    events (same shape run_restic(capture_json=True) would have returned)."""
    env = _repo_env(repo_path, password, password_file)
    cmd = [RESTIC_BIN] + list(global_args or []) + list(args)
    try:
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as e:
        raise ResticError(f"restic binary not found: {RESTIC_BIN}") from e

    events = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        if event.get("message_type") == "status" and on_progress:
            on_progress(event)

    stderr = proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
    returncode = proc.wait()

    if returncode != 0:
        raise ResticError(
            f"restic {' '.join(args)} failed (exit {returncode}): {stderr.strip()}"
        )
    return events


def backup(repo_path, paths, tags=None, exclude_patterns=None, password=None,
           password_file=None, dry_run=False, extra_args=None, global_args=None,
           one_file_system=False, exclude_caches=False, exclude_if_present=None,
           exclude_larger_than=None, host=None, read_concurrency=None,
           on_progress=None):
    """
    Run `restic backup` and return the parsed summary dict, containing (among
    others) `snapshot_id`, `files_new`, `files_changed`, `files_unmodified`,
    `data_added`, `total_bytes_processed`, `total_files_processed`.

    one_file_system: don't cross filesystem/mount boundaries.
    exclude_caches: skip directories tagged with a CACHEDIR.TAG file.
    exclude_if_present: list of "FILENAME" or "FILENAME:CONTENTS" markers —
        skip any directory containing a matching file.
    exclude_larger_than: skip files above this size (e.g. "1G", "500M").
    host: override the hostname recorded on the snapshot (defaults to this
        machine's hostname if unset).
    read_concurrency: number of concurrent file reads during backup.
    on_progress: if given, called with each raw `message_type":"status"` JSON
        dict restic emits while running (fields include percent_done,
        bytes_done, total_bytes, seconds_remaining) — best-effort live
        progress; throttling/reporting to the dashboard is the caller's job.

    Raises ResticError on failure, or if no summary line is found in output.
    """
    args = ["backup", "--json"]
    for tag in (tags or []):
        args += ["--tag", tag]
    for pattern in (exclude_patterns or []):
        args += ["--exclude", pattern]
    if dry_run:
        args.append("--dry-run")
    if one_file_system:
        args.append("--one-file-system")
    if exclude_caches:
        args.append("--exclude-caches")
    for marker in (exclude_if_present or []):
        args += ["--exclude-if-present", marker]
    if exclude_larger_than:
        args += ["--exclude-larger-than", exclude_larger_than]
    if host:
        args += ["--host", host]
    if read_concurrency:
        args += ["--read-concurrency", str(read_concurrency)]
    args += list(extra_args or [])
    args += list(paths)

    if on_progress is not None:
        events = _run_restic_backup_streaming(
            args, repo_path, password, password_file,
            global_args=global_args, on_progress=on_progress,
        )
    else:
        events = run_restic(args, repo_path, password, password_file,
                             capture_json=True, global_args=global_args)
        events = events if isinstance(events, list) else [events]

    summary = next((e for e in events if e.get("message_type") == "summary"), None)
    if summary is None:
        raise ResticError("restic backup completed but produced no summary event")
    return summary


def forget_prune(repo_path, keep_last=0, keep_hourly=0, keep_daily=0, keep_weekly=0,
                  keep_monthly=0, keep_yearly=0, keep_within=None, group_by=None,
                  tags=None, extra_args=None, password=None, password_file=None,
                  global_args=None):
    """Run `restic forget --prune`, scoped to `tags` if given so this job's
    retention policy never prunes another job's snapshots in the same repo.

    Returns the list of full snapshot IDs restic actually removed (parsed
    from `--json` output's per-group `remove` lists), so callers can report
    them to the dashboard via /api/monitoring/target-purged. Returns an
    empty list if nothing was removed."""
    args = ["forget", "--prune", "--json"]
    if keep_last:
        args += ["--keep-last", str(keep_last)]
    if keep_hourly:
        args += ["--keep-hourly", str(keep_hourly)]
    if keep_daily:
        args += ["--keep-daily", str(keep_daily)]
    if keep_weekly:
        args += ["--keep-weekly", str(keep_weekly)]
    if keep_monthly:
        args += ["--keep-monthly", str(keep_monthly)]
    if keep_yearly:
        args += ["--keep-yearly", str(keep_yearly)]
    if keep_within:
        args += ["--keep-within", keep_within]
    if group_by:
        args += ["--group-by", group_by]
    for tag in (tags or []):
        args += ["--tag", tag]
    args += list(extra_args or [])

    events = run_restic(args, repo_path, password, password_file,
                         capture_json=True, global_args=global_args)
    events = events if isinstance(events, list) else ([events] if events else [])

    removed_ids = []
    for event in events:
        for snap in (event.get("remove") or []):
            snap_id = snap.get("id") if isinstance(snap, dict) else None
            if snap_id:
                removed_ids.append(snap_id)
    return removed_ids


def list_snapshots(repo_path, tags=None, password=None, password_file=None):
    """Return the list of snapshot dicts (`restic snapshots --json`)."""
    args = ["snapshots", "--json"]
    for tag in (tags or []):
        args += ["--tag", tag]
    result = run_restic(args, repo_path, password, password_file, capture_json=True)
    return result if isinstance(result, list) else ([result] if result else [])


def list_files(repo_path, snapshot_id, path=None, password=None, password_file=None):
    """Return the list of file entry dicts for a snapshot (`restic ls --json`)."""
    args = ["ls", "--json", snapshot_id]
    if path:
        args.append(path)
    result = run_restic(args, repo_path, password, password_file, capture_json=True)
    result = result if isinstance(result, list) else ([result] if result else [])
    # First line is a "snapshot" message_type; the rest are "file"/"dir" entries.
    return [e for e in result if e.get("struct_type") in ("node",) or "path" in e]


def find(repo_path, pattern, snapshot_id=None, password=None, password_file=None,
          ignore_case=True):
    """Return matches for `restic find --json <pattern>`, optionally within one
    snapshot. `pattern` is a glob (restic's own filepath.Match syntax, not a
    substring search) — wrap it in '*' yourself for a "contains" search."""
    args = ["find", "--json"]
    if ignore_case:
        args.append("--ignore-case")
    args.append(pattern)
    if snapshot_id:
        args += ["--snapshot", snapshot_id]
    result = run_restic(args, repo_path, password, password_file, capture_json=True)
    return result if isinstance(result, list) else ([result] if result else [])


def restore(repo_path, snapshot_id, target_dir, include_path=None, password=None, password_file=None):
    """Restore a snapshot (or one path within it) to target_dir."""
    args = ["restore", snapshot_id, "--target", target_dir]
    if include_path:
        args += ["--include", include_path]
    run_restic(args, repo_path, password, password_file)


def unlock(repo_path, password=None, password_file=None):
    """Remove stale restic repository locks (manual recovery only)."""
    run_restic(["unlock"], repo_path, password, password_file)


def check(repo_path, password=None, password_file=None, read_data=False,
          read_data_subset=None, global_args=None):
    """
    Run `restic check` to verify repository structure/index consistency.

    By default this is metadata-only (no data blobs read back) and cheap
    enough to run after every backup. Pass read_data=True for a full data
    read-back (slow, reads the entire repo), or read_data_subset (e.g. "5%"
    or "500M") to only verify a portion of the data on each run — both are
    intended for manual/occasional use, not after every backup.

    Raises ResticError if the repository is inconsistent/corrupt.
    """
    args = ["check"]
    if read_data_subset:
        args += [f"--read-data-subset={read_data_subset}"]
    elif read_data:
        args.append("--read-data")
    run_restic(args, repo_path, password, password_file, global_args=global_args)
