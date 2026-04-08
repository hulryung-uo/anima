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
MAX_LOCK_AGE = 1800  # 30 minutes


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


def is_fix_locked(problem: str) -> bool:
    """True if another session is actively fixing the given problem.

    Returns False if the lock file is missing, malformed, or stale (older
    than MAX_LOCK_AGE).  SHA-based invalidation is handled by
    acquire_fix_lock so that is_fix_locked can be used for simple presence
    checks without needing a current SHA.
    """
    data = _read_lock()
    if not data:
        return False
    if data.get("problem") != problem:
        return False
    # Stale by age?
    started = data.get("started", 0)
    if time.time() - started > MAX_LOCK_AGE:
        return False
    return True


def acquire_fix_lock(problem: str, pid: int, sha: str) -> bool:
    """Try to acquire the fix lock for `problem`. Returns False on conflict.

    A lock held for the same problem is treated as stale (and therefore
    acquirable) if the stored SHA differs from the incoming SHA — this
    signals that a commit has been made since the lock was created, meaning
    the problem was likely already fixed.
    """
    data = _read_lock()
    if data and data.get("problem") == problem:
        # Check age
        started = data.get("started", 0)
        if time.time() - started <= MAX_LOCK_AGE:
            # Check SHA: if locked SHA matches incoming SHA, it's a conflict.
            # If they differ, the commit moved on — allow re-acquisition.
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
