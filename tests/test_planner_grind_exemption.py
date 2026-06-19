"""Anti-thrash: the productive-grind exemption must use the live result.

Regression guard for the dominant-procedure exemption in the planner health
loop. The exemption used to suppress the 60s health break whenever the
dominant procedure's (possibly stale) repeat counter was 0, which let a
thrash between a *failing* procedure and a fallback escape detection. The fix
keys the exemption on the most-recent tick's success.
"""
from __future__ import annotations

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry, ProcedureResult


def _planner() -> Planner:
    return Planner(ProcedureRegistry())


def test_succeeding_grind_is_exempt():
    """A succeeding dominant procedure is a productive grind -> exempt."""
    p = _planner()
    p._repeat_counter["hunt_nearby"] = 0
    ok = ProcedureResult(success=True, message="kill")
    assert p._is_productive_grind("hunt_nearby", ok) is True


def test_failing_latest_tick_is_not_exempt():
    """Latest tick failed -> stuck loop, must NOT be exempted.

    This is the core bug: the dominant procedure's stale counter is 0 (it
    last succeeded long ago and has not re-run), but the planner is now
    thrashing and the most recent run failed. The old `repeat_counter == 0`
    test wrongly exempted this; the live-result test correctly does not.
    """
    p = _planner()
    p._repeat_counter["hunt_nearby"] = 0  # stale leftover 0
    failed = ProcedureResult(success=False, message="no target")
    assert p._is_productive_grind("hunt_nearby", failed) is False


def test_no_result_is_not_exempt():
    """An idle tick (nothing ran) is never a productive grind."""
    p = _planner()
    p._repeat_counter["hunt_nearby"] = 0
    assert p._is_productive_grind("hunt_nearby", None) is False


def test_dominant_with_consecutive_failures_is_not_exempt():
    """Even if some tick succeeded, a dominant proc carrying failures is stuck."""
    p = _planner()
    p._repeat_counter["mine_ore"] = 3  # dominant has been failing
    ok = ProcedureResult(success=True, message="other proc ok")
    assert p._is_productive_grind("mine_ore", ok) is False


def test_no_dominant_is_not_exempt():
    p = _planner()
    ok = ProcedureResult(success=True)
    assert p._is_productive_grind(None, ok) is False
