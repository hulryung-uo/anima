"""Regression tests for the post-turn movement throttle.

A pure facing change (turn) must hold the walker for the full
``TURN_DELAY_MS`` (100 ms) before the follow-up move packet is allowed,
matching ClassicUO's ``Constants.TURN_DELAY``.  The turn send sites used to
bake in a 50 ms magic number, which let ``can_walk()`` return ``True`` roughly
half the turn delay early — the very server-side throttling that delay exists
to avoid.
"""

from __future__ import annotations

from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import TURN_DELAY_MS, WalkerManager, _now_ms


def _make_walker() -> WalkerManager:
    return WalkerManager(SelfState(serial=1), EventStream())


def test_mark_turn_sets_full_turn_delay():
    """mark_turn() must throttle for TURN_DELAY_MS, not the old 50 ms."""
    w = _make_walker()
    before = _now_ms()
    w.mark_turn()
    cooldown = w.last_step_time - before
    # The cooldown must be (about) the full turn delay. A tiny slack absorbs
    # the clock tick between the two _now_ms() reads.
    assert cooldown >= TURN_DELAY_MS - 5
    # And it must NOT be the old, too-short 50 ms value.
    assert cooldown > 60


def test_can_walk_blocked_during_turn_delay():
    """A move requested 60 ms after a turn (past the old 50 ms) must wait."""
    w = _make_walker()
    w.mark_turn()
    # Simulate "60 ms have elapsed": past the buggy 50 ms cutoff but still
    # inside the real 100 ms turn delay. The follow-up move must be blocked.
    w.last_step_time -= 60
    assert w.can_walk() is False


def test_can_walk_allowed_after_full_turn_delay():
    """Once the full turn delay elapses, the move is allowed."""
    w = _make_walker()
    w.mark_turn()
    # Simulate the full turn delay (plus a hair) having elapsed.
    w.last_step_time -= TURN_DELAY_MS + 1
    assert w.can_walk() is True


def test_mark_turn_matches_classicuo_constant():
    """TURN_DELAY_MS mirrors ClassicUO's Constants.TURN_DELAY (100 ms)."""
    assert TURN_DELAY_MS == 100
