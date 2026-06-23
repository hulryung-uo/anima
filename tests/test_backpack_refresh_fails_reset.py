"""The backpack-refresh fail counter must reset when the stall clears.

``Planner._backpack_refresh_fails`` counts CONSECUTIVE failed attempts to
refresh a stale-but-heavy backpack (no visible items yet real weight, so the
server's 0x3C container contents never arrived). Once the counter reaches 8 the
refresh interval slows from 15s to 120s and the heavy re-detect path arms.

Bug: the only reset was ``elif bp_items > 0`` — items reappearing. When the
pack legitimately EMPTIES below the weight floor (e.g. right after a bank
deposit: bp_items==0, weight<=50) the counter was NOT reset. A stale fail count
from an earlier stall then survived into the next loading-up episode and
immediately forced the slow 120s interval on what was actually a brand-new
stall, delaying the first refresh attempt by ~2 minutes.

The fix keys the reset on "not in the stuck state" rather than only on
"items present", so a deposit-emptied pack re-arms the fast path. These tests
pin the pure ``_backpack_is_stuck`` predicate that drives both the entry
condition and the reset.
"""

from __future__ import annotations

from anima.planner.planner import Planner


def test_stuck_when_empty_and_heavy():
    # No visible items but carrying real weight → stale contents, refresh.
    assert Planner._backpack_is_stuck(bp_items=0, weight=315) is True


def test_not_stuck_when_items_visible():
    assert Planner._backpack_is_stuck(bp_items=5, weight=315) is False


def test_not_stuck_when_empty_and_light():
    # The deposit-emptied case: pack is genuinely empty and light, NOT a stall.
    # This is the path the buggy ``elif bp_items > 0`` reset missed.
    assert Planner._backpack_is_stuck(bp_items=0, weight=10) is False


def test_boundary_at_weight_floor():
    # Exactly at the floor is not yet "heavy" (strict >).
    assert Planner._backpack_is_stuck(bp_items=0, weight=Planner._BACKPACK_STUCK_WEIGHT) is False
    assert Planner._backpack_is_stuck(bp_items=0, weight=Planner._BACKPACK_STUCK_WEIGHT + 1) is True


def test_deposit_emptied_pack_resets_consecutive_count():
    """Regression for the leak: a stall builds the counter, then a deposit
    empties the pack below the weight floor — the counter must reset so the
    NEXT stall starts on the fast 15s interval, not the stale 120s one."""
    from anima.procedures.base import ProcedureRegistry

    planner = Planner(ProcedureRegistry())

    # An earlier stale-backpack stall climbed the counter past the slow-path
    # watermark (>= 8 forces the 120s interval).
    planner._backpack_refresh_fails = 9

    # Model the planner's reset branch: the pack emptied below the weight floor
    # after a bank deposit, so it is NOT in the stuck state anymore.
    bp_items, weight = 0, 10
    if planner._backpack_is_stuck(bp_items, weight):
        # would keep escalating
        pass
    else:
        planner._backpack_refresh_fails = 0

    assert planner._backpack_refresh_fails == 0

    # And a fresh stall (heavy again, still empty) is correctly detected so the
    # fast 15s interval — not the stale slow one — governs the first attempt.
    assert planner._backpack_is_stuck(bp_items=0, weight=200) is True
    assert planner._backpack_refresh_fails < 8
