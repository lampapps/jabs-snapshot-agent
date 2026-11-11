"""A lock file per job, so two runs of the same job never overlap (a tick and a manual `run`).

There is no local database (restic's repository index is the source of truth), so a plain
portalocker lock file per job is all that is needed.
"""

import json
import os
import socket
import sys
import time

import portalocker

from . import settings


def acquire_lock(lock_path):
    """Take an exclusive lock on lock_path and return the open file; RuntimeError if it is held."""
    lock_dir = os.path.dirname(lock_path)
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Could not create lock directory: {lock_dir}") from exc

    # A lock file from a crashed run is cleared before locking.
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as existing:
                info = json.load(existing)
            if time.time() - info.get("created_at", 0) > settings.STALE_LOCK_S:
                os.remove(lock_path)
        except (OSError, json.JSONDecodeError, ValueError):
            try:
                os.remove(lock_path)
            except OSError:
                pass

    try:
        lock_file = open(lock_path, "a+", encoding="utf-8")
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        json.dump({
            "pid": os.getpid(),
            "created_at": time.time(),
            "hostname": socket.gethostname(),
            "command": " ".join(["python"] + [os.path.basename(arg) for arg in sys.argv]),
        }, lock_file)
        lock_file.flush()
        return lock_file
    except portalocker.exceptions.LockException as exc:
        detail = "Lock exists but details couldn't be read"
        try:
            with open(lock_path, "r", encoding="utf-8") as existing:
                info = json.load(existing)
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.get("created_at", 0)))
            detail = f"Lock held by PID {info.get('pid', 'unknown')} since {created}"
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        raise RuntimeError(f"Could not acquire lock: {lock_path}. {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"Error acquiring lock: {lock_path}") from exc


def release_lock(lock_file) -> None:
    lock_path = lock_file.name
    try:
        portalocker.unlock(lock_file)
    except (portalocker.exceptions.LockException, OSError):
        pass
    finally:
        lock_file.close()
    try:
        os.remove(lock_path)
    except OSError:
        pass
