"""Tests for flee-threshold hysteresis in the melee combat loop.

HP%% is read off mobile-status packets and can dip for a single tick (a damage
packet landing just before its triggered bandage resolves, or a momentary
status-bar glitch). ``_should_retreat`` debounces that: a lone sub-floor reading
arms a strike but does not break the engagement — HP must stay below the retreat
floor for ``RETREAT_CONFIRM_TICKS`` consecutive ticks. A tick at/above the floor
clears the strike count so a recovered blip never carries into a later drop.
"""
from types import SimpleNamespace

from anima.procedures.combat_loop import (
    RETREAT_CONFIRM_TICKS,
    RETREAT_HP_PCT,
    _should_retreat,
)


def _ctx():
    return SimpleNamespace(blackboard={})


class TestShouldRetreat:
    def test_single_dip_does_not_retreat(self):
        # One sub-floor reading arms a strike but must NOT break the engagement.
        ctx = _ctx()
        assert RETREAT_CONFIRM_TICKS >= 2  # premise: a single dip is debounced
        assert _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100) is False

    def test_consecutive_dips_retreat(self):
        # Sustained sub-floor HP across the required ticks → retreat.
        ctx = _ctx()
        results = [
            _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100)
            for _ in range(RETREAT_CONFIRM_TICKS)
        ]
        assert results[:-1] == [False] * (RETREAT_CONFIRM_TICKS - 1)
        assert results[-1] is True

    def test_recovery_clears_strikes(self):
        # A transient dip then recovery must NOT leave a latent strike: a later
        # lone dip starts fresh and does not immediately trip the retreat.
        ctx = _ctx()
        assert _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100) is False
        # HP recovers above the floor → strike count resets.
        assert _should_retreat(ctx, RETREAT_HP_PCT + 5.0, hits_max=100) is False
        assert ctx.blackboard["_retreat_strikes"] == 0
        # A new lone dip is again just a single strike, not a retreat.
        assert _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100) is False

    def test_at_floor_is_not_below(self):
        # Exactly at the floor counts as safe (matches the original `< floor`).
        ctx = _ctx()
        for _ in range(RETREAT_CONFIRM_TICKS + 1):
            assert _should_retreat(ctx, RETREAT_HP_PCT, hits_max=100) is False

    def test_unknown_hp_never_retreats(self):
        # hits_max <= 0 means HP is unknown (un-queried) — never retreat, and
        # the strike count stays cleared.
        ctx = _ctx()
        ctx.blackboard["_retreat_strikes"] = 1
        assert _should_retreat(ctx, 0.0, hits_max=0) is False
        assert ctx.blackboard["_retreat_strikes"] == 0
