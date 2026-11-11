"""Thin subprocess wrapper around the restic CLI.

The repository and password reach restic through environment variables, never as arguments, so
they never show up in `ps` output or logs. A non-zero exit raises ResticError with restic's
stderr attached; callers decide how to handle it.
"""

import json
import os
import subprocess
from typing import Callable, Optional

from . import settings


class ResticError(RuntimeError):
    """Raised when a restic invocation exits non-zero."""


def build_global_args(limit_upload=0, limit_download=0, compression=None, pack_size=None,
                      cache_dir=None, no_cache=False, retry_lock=None) -> list:
    """Restic's global flags, which must come before the subcommand.

    limit_upload / limit_download are KiB/s (0 = unlimited); pack_size is MiB; retry_lock is a
    duration such as "5m" to wait for a repository lock instead of failing at once.
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


def _repo_env(repo_path: str, password: Optional[str] = None, password_file: Optional[str] = None) -> dict:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = repo_path
    # Only one of the two should be set; the file wins.
    if password_file:
        env["RESTIC_PASSWORD_FILE"] = password_file
        env.pop("RESTIC_PASSWORD", None)
    elif password:
        env["RESTIC_PASSWORD"] = password
        env.pop("RESTIC_PASSWORD_FILE", None)
    return env


def run_restic(args, repo_path, password=None, password_file=None, capture_json=False, global_args=None):
    """Run `restic <args>` against repo_path and return stdout (or parsed JSON when capture_json)."""
    env = _repo_env(repo_path, password, password_file)
    binary = settings.restic_bin()
    try:
        result = subprocess.run([binary, *(global_args or []), *args], env=env, capture_output=True,
                                text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise ResticError(f"restic {' '.join(args)} failed (exit {exc.returncode}): {exc.stderr.strip()}") from exc
    except FileNotFoundError as exc:
        raise ResticError(f"restic binary not found: {binary}") from exc
    return parse_json_output(result.stdout) if capture_json else result.stdout


def parse_json_output(stdout: str):
    """Parse restic --json output: one JSON value, or newline-delimited objects returned as a list.

    Plain-text lines are skipped; restic prints some warnings (e.g. pruning without a cache) on stdout.
    """
    values = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _as_list(result) -> list:
    return result if isinstance(result, list) else ([result] if result else [])


def repo_exists(repo_path, password=None, password_file=None, global_args=None) -> bool:
    try:
        run_restic(["cat", "config"], repo_path, password, password_file, global_args=global_args)
        return True
    except ResticError:
        return False


def init_repo(repo_path, password=None, password_file=None, global_args=None) -> None:
    run_restic(["init"], repo_path, password, password_file, global_args=global_args)


def _run_backup_streaming(args, repo_path, password=None, password_file=None, global_args=None,
                          on_progress: Optional[Callable] = None) -> list:
    """Like run_restic(capture_json=True) but calls on_progress(dict) for each live status line."""
    env = _repo_env(repo_path, password, password_file)
    binary = settings.restic_bin()
    try:
        proc = subprocess.Popen([binary, *(global_args or []), *args], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise ResticError(f"restic binary not found: {binary}") from exc

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
        raise ResticError(f"restic {' '.join(args)} failed (exit {returncode}): {stderr.strip()}")
    return events


def backup(repo_path, paths, tags=None, exclude_patterns=None, password=None, password_file=None,
           dry_run=False, extra_args=None, global_args=None, one_file_system=False, exclude_caches=False,
           exclude_if_present=None, exclude_larger_than=None, host=None, read_concurrency=None,
           on_progress=None) -> dict:
    """Run `restic backup` and return its summary event (snapshot_id, files_new, data_added, ...)."""
    args = ["backup", "--json"]
    for tag in tags or []:
        args += ["--tag", tag]
    for pattern in exclude_patterns or []:
        args += ["--exclude", pattern]
    if dry_run:
        args.append("--dry-run")
    if one_file_system:
        args.append("--one-file-system")
    if exclude_caches:
        args.append("--exclude-caches")
    for marker in exclude_if_present or []:
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
        events = _run_backup_streaming(args, repo_path, password, password_file, global_args=global_args,
                                       on_progress=on_progress)
    else:
        events = _as_list(run_restic(args, repo_path, password, password_file, capture_json=True,
                                     global_args=global_args))
    summary = next((e for e in events if e.get("message_type") == "summary"), None)
    if summary is None:
        raise ResticError("restic backup completed but produced no summary event")
    return summary


def forget_prune(repo_path, keep_last=0, keep_hourly=0, keep_daily=0, keep_weekly=0, keep_monthly=0,
                 keep_yearly=0, keep_within=None, group_by=None, tags=None, extra_args=None, password=None,
                 password_file=None, global_args=None) -> list:
    """Run `restic forget --prune` scoped to tags; returns the full ids of the snapshots removed."""
    args = ["forget", "--prune", "--json"]
    for flag, value in (("--keep-last", keep_last), ("--keep-hourly", keep_hourly), ("--keep-daily", keep_daily),
                        ("--keep-weekly", keep_weekly), ("--keep-monthly", keep_monthly),
                        ("--keep-yearly", keep_yearly)):
        if value:
            args += [flag, str(value)]
    if keep_within:
        args += ["--keep-within", keep_within]
    if group_by:
        args += ["--group-by", group_by]
    for tag in tags or []:
        args += ["--tag", tag]
    args += list(extra_args or [])

    events = _as_list(run_restic(args, repo_path, password, password_file, capture_json=True,
                                 global_args=global_args))
    removed = []
    for event in events:
        for snap in event.get("remove") or []:
            snap_id = snap.get("id") if isinstance(snap, dict) else None
            if snap_id:
                removed.append(snap_id)
    return removed


def list_snapshots(repo_path, tags=None, password=None, password_file=None, global_args=None) -> list:
    args = ["snapshots", "--json"]
    for tag in tags or []:
        args += ["--tag", tag]
    return _as_list(run_restic(args, repo_path, password, password_file, capture_json=True,
                               global_args=global_args))


def list_files(repo_path, snapshot_id, path=None, password=None, password_file=None, global_args=None) -> list:
    """Entries of a snapshot (`restic ls --json`), without the leading snapshot record."""
    args = ["ls", "--json", snapshot_id]
    if path:
        args.append(path)
    result = _as_list(run_restic(args, repo_path, password, password_file, capture_json=True,
                                 global_args=global_args))
    return [e for e in result if e.get("struct_type") == "node" or "path" in e]


def find(repo_path, pattern, snapshot_id=None, password=None, password_file=None, ignore_case=True,
         global_args=None) -> list:
    """`restic find --json`; pattern is a glob, so wrap it in '*' for a contains search."""
    args = ["find", "--json"]
    if ignore_case:
        args.append("--ignore-case")
    args.append(pattern)
    if snapshot_id:
        args += ["--snapshot", snapshot_id]
    return _as_list(run_restic(args, repo_path, password, password_file, capture_json=True,
                               global_args=global_args))


def restore(repo_path, snapshot_id, target_dir, include_path=None, password=None, password_file=None,
            global_args=None) -> None:
    args = ["restore", snapshot_id, "--target", target_dir]
    if include_path:
        args += ["--include", include_path]
    run_restic(args, repo_path, password, password_file, global_args=global_args)


def unlock(repo_path, password=None, password_file=None, global_args=None) -> None:
    run_restic(["unlock"], repo_path, password, password_file, global_args=global_args)


def check(repo_path, password=None, password_file=None, read_data=False, read_data_subset=None,
          global_args=None) -> None:
    """Verify repository consistency; metadata only unless read_data or a data subset is requested."""
    args = ["check"]
    if read_data_subset:
        args.append(f"--read-data-subset={read_data_subset}")
    elif read_data:
        args.append("--read-data")
    run_restic(args, repo_path, password, password_file, global_args=global_args)


def stats(repo_path, mode="restore-size", snapshot=None, tags=None, host=None, paths=None, password=None,
          password_file=None, global_args=None) -> dict:
    """`restic stats --json`: {"total_size": bytes, "total_file_count": n, ...}.

    mode raw-data measures the repository itself; restore-size measures what restoring the
    selected snapshot(s) would write. snapshot may be an id or "latest"; tags, host and paths
    narrow which snapshots count (tags join with AND, as restic's comma form does).
    """
    args = ["stats", "--json", "--mode", mode]
    if tags:
        args += ["--tag", ",".join(tags)]
    if host:
        args += ["--host", host]
    for path in paths or []:
        args += ["--path", path]
    if snapshot:
        args.append(snapshot)
    result = run_restic(args, repo_path, password, password_file, capture_json=True, global_args=global_args)
    return result if isinstance(result, dict) else {}


def global_args_from(options) -> list:
    """Global restic flags from a ResticOptions."""
    return build_global_args(
        limit_upload=options.limit_upload, limit_download=options.limit_download,
        compression=options.compression or None, pack_size=options.pack_size or None,
        cache_dir=options.cache_dir or None, no_cache=options.no_cache, retry_lock=options.retry_lock or None)


def backup_kwargs_from(options) -> dict:
    """backup() tuning arguments from a ResticOptions."""
    return dict(
        extra_args=options.extra_backup_args, one_file_system=options.one_file_system,
        exclude_caches=options.exclude_caches, exclude_if_present=options.exclude_if_present,
        exclude_larger_than=options.exclude_larger_than or None, host=options.host or None,
        read_concurrency=options.read_concurrency or None)
