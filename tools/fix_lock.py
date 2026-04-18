"""File-based lock to prevent duplicate Claude Code fix sessions.

When the supervisor launches a Claude Code session to investigate a
problem, it writes a lock file naming the problem, the session PID, and
the git SHA it was working from. A second process (human or another
supervisor instance) checking the lock will see the problem is already
being worked on and skip.

The lock is automatically ignored if:
  - the git SHA has moved on (the problem was already fixed)
  - the lock is older than MAX_LOCK_AGE (the session crashed)
  - the file is missing or malformed
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
LOCK_FILE = ROOT / "data" / "fixing.json"

# Lock is considered stale after this many seconds (covers crashed sessions)
MAX_LOCK_AGE = 1200  # 20 minutes


def _read_lock() -> dict | None:
    try:
        return json.loads(LOCK_FILE.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _current_sha() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID on POSIX.

    Uses kill(pid, 0): returns True if the signal could be delivered,
    False if the process doesn't exist. Treats EPERM (different owner)
    as alive to avoid false negatives. On error, returns True so we err
    on the side of honoring the lock.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def is_fix_locked(problem: str) -> bool:
    """True if another session is actively fixing the given problem.

    Returns False if the lock file is missing, malformed, stale by age,
    or owned by a dead PID.
    """
    data = _read_lock()
    if not data:
        return False
    if data.get("problem") != problem:
        return False
    started = data.get("started", 0)
    if time.time() - started > MAX_LOCK_AGE:
        return False
    pid = data.get("pid", 0)
    if pid and not _pid_alive(pid):
        return False
    return True


def acquire_fix_lock(problem: str, pid: int, sha: str) -> bool:
    """Try to acquire the fix lock for `problem`. Returns False on conflict.

    A lock held for the same problem is treated as stale if any of:
      - age > MAX_LOCK_AGE
      - stored PID is not alive (process crashed/orphaned)
      - stored SHA differs from incoming SHA (commit moved on)
    """
    data = _read_lock()
    if data and data.get("problem") == problem:
        started = data.get("started", 0)
        if time.time() - started <= MAX_LOCK_AGE:
            locked_pid = data.get("pid", 0)
            if locked_pid and not _pid_alive(locked_pid):
                # Process gone — allow re-acquisition.
                pass
            else:
                locked_sha = data.get("sha", "")
                if locked_sha and sha and locked_sha == sha:
                    return False
                elif not locked_sha or not sha:
                    # No SHA info available — treat as conflict to be safe.
                    return False
                # SHAs differ → lock is stale due to new commit, fall through.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "problem": problem,
        "pid": pid,
        "sha": sha,
        "started": time.time(),
    }, indent=2))
    return True


def release_fix_lock() -> None:
    """Remove the lock file (ignored if it doesn't exist)."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def sweep_stale_lock() -> bool:
    """Remove the lock file if it's stale (age or dead PID).

    Called by the supervisor on startup so that a crashed session's lock
    can't block a freshly-started supervisor. Returns True if a stale
    lock was removed.
    """
    data = _read_lock()
    if not data:
        return False
    started = data.get("started", 0)
    pid = data.get("pid", 0)
    stale_age = time.time() - started > MAX_LOCK_AGE
    stale_pid = bool(pid) and not _pid_alive(pid)
    if stale_age or stale_pid:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        return True
    return False


class FixLock:
    """Context manager for acquire/release.

    Usage:
        with FixLock("procedure_spam:mine_ore", pid=os.getpid(), sha="abc") as ok:
            if ok:
                run_claude_code()
    """

    def __init__(self, problem: str, pid: int | None = None, sha: str = "") -> None:
        self._problem = problem
        self._pid = pid if pid is not None else os.getpid()
        self._sha = sha or _current_sha()
        self._owned = False

    def __enter__(self) -> bool:
        self._owned = acquire_fix_lock(self._problem, self._pid, self._sha)
        return self._owned

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned:
            release_fix_lock()
