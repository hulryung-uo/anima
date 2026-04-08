"""Tests for the fix lock helper."""
import json
import time
from unittest.mock import patch

import pytest

from tools.fix_lock import (
    FixLock, acquire_fix_lock, release_fix_lock, is_fix_locked,
)


@pytest.fixture
def tmp_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "fixing.json"
    monkeypatch.setattr("tools.fix_lock.LOCK_FILE", lock_path)
    return lock_path


class TestFixLock:
    def test_acquire_creates_file(self, tmp_lock):
        ok = acquire_fix_lock("procedure_spam:mine_ore", pid=12345, sha="abc1234")
        assert ok is True
        assert tmp_lock.exists()
        data = json.loads(tmp_lock.read_text())
        assert data["problem"] == "procedure_spam:mine_ore"
        assert data["pid"] == 12345
        assert data["sha"] == "abc1234"

    def test_acquire_conflicts_on_same_problem(self, tmp_lock):
        assert acquire_fix_lock("X", pid=1, sha="abc") is True
        assert acquire_fix_lock("X", pid=2, sha="abc") is False

    def test_acquire_succeeds_after_sha_changed(self, tmp_lock):
        # First session fixed the problem at abc → new session at def should
        # be allowed even if the lock was never released.
        assert acquire_fix_lock("X", pid=1, sha="abc") is True
        with patch("tools.fix_lock._current_sha", return_value="def"):
            assert acquire_fix_lock("X", pid=2, sha="def") is True

    def test_release_removes_file(self, tmp_lock):
        acquire_fix_lock("X", pid=1, sha="abc")
        release_fix_lock()
        assert not tmp_lock.exists()

    def test_is_fix_locked(self, tmp_lock):
        assert is_fix_locked("X") is False
        acquire_fix_lock("X", pid=1, sha="abc")
        assert is_fix_locked("X") is True
        assert is_fix_locked("Y") is False  # different problem

    def test_stale_lock_expires(self, tmp_lock):
        """A lock older than MAX_LOCK_AGE is ignored."""
        from tools.fix_lock import MAX_LOCK_AGE
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 999999, "sha": "abc",
            "started": time.time() - MAX_LOCK_AGE - 10,
        }))
        assert is_fix_locked("X") is False
        # New lock should acquire successfully
        assert acquire_fix_lock("X", pid=1, sha="abc") is True

    def test_missing_file_not_locked(self, tmp_lock):
        assert is_fix_locked("anything") is False

    def test_malformed_file_not_locked(self, tmp_lock):
        tmp_lock.write_text("not json")
        assert is_fix_locked("X") is False

    def test_context_manager(self, tmp_lock):
        with FixLock("X", pid=1, sha="abc") as ok:
            assert ok is True
            assert is_fix_locked("X") is True
        assert is_fix_locked("X") is False

    def test_context_manager_conflict(self, tmp_lock):
        acquire_fix_lock("X", pid=1, sha="abc")
        with FixLock("X", pid=2, sha="abc") as ok:
            assert ok is False
        # Outer lock still held
        assert is_fix_locked("X") is True
