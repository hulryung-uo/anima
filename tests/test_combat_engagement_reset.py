"""A fresh hunt must not inherit the previous engagement's flee timer.

``_should_retreat`` and ``_bandage_trigger_pct`` accumulate cross-tick state in
the blackboard (the consecutive-sub-floor strike count, and the last HP%/ts used
to estimate DPS). That carry-over is correct *within* a fight but a hunt that
breaks off at the retreat floor leaves ``_retreat_strikes`` parked at
``RETREAT_CONFIRM_TICKS``. The agent heals and re-enters ``hunt_nearby`` — and
the first transient sub-floor HP read of the *new* fight would instantly trip a
retreat, flapping the agent off a winnable fight. ``_reset_engagement_state``
(called at the top of ``HuntNearby.execute``) clears that scratch state so the
debounce starts counting fresh.
"""
from types import SimpleNamespace

from anima.procedures.combat_loop import (
    RETREAT_CONFIRM_TICKS,
    RETREAT_HP_PCT,
    _bandage_trigger_pct,
    _reset_engagement_state,
    _should_retreat,
)


def _ctx(**blackboard):
    return SimpleNamespace(blackboard=dict(blackboard))


class TestResetEngagementState:
    def test_clears_armed_retreat_strikes(self):
        # A previous fight broke off at the floor: strikes parked at the
        # confirm threshold.
        ctx = _ctx(_retreat_strikes=RETREAT_CONFIRM_TICKS)
        _reset_engagement_state(ctx)
        assert ctx.blackboard["_retreat_strikes"] == 0

    def test_clears_hp_trajectory_keys(self):
        ctx = _ctx(_bandage_hp_last=40.0, _bandage_hp_ts=1000.0)
        _reset_engagement_state(ctx)
        assert "_bandage_hp_last" not in ctx.blackboard
        assert "_bandage_hp_ts" not in ctx.blackboard

    def test_is_a_clean_noop_on_first_ever_fight(self):
        # No prior state — reset just establishes the cleared baseline.
        ctx = _ctx()
        _reset_engagement_state(ctx)
        assert ctx.blackboard["_retreat_strikes"] == 0


class TestStrikeStateDoesNotLeakAcrossEngagements:
    def test_stale_strikes_would_retreat_on_a_single_dip(self):
        """Regression premise: WITHOUT a reset, a strike count left armed from a
        prior retreat trips the new fight's retreat on the very first sub-floor
        read — abandoning a winnable engagement."""
        # Prior engagement broke off: one strike short of nothing — i.e. parked
        # at RETREAT_CONFIRM_TICKS - 1, so a single new dip reaches the floor.
        ctx = _ctx(_retreat_strikes=RETREAT_CONFIRM_TICKS - 1)
        # One lone sub-floor read (a transient, e.g. pre-heal stale status bar).
        assert _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100) is True

    def test_reset_lets_the_debounce_start_fresh(self):
        """WITH the reset, the same single dip only arms one strike and does NOT
        retreat — the debounce behaves as on the first-ever fight."""
        ctx = _ctx(_retreat_strikes=RETREAT_CONFIRM_TICKS - 1)
        _reset_engagement_state(ctx)
        # First sub-floor read of the new fight: one strike, no retreat.
        assert RETREAT_CONFIRM_TICKS >= 2  # premise: a single dip is debounced
        assert _should_retreat(ctx, RETREAT_HP_PCT - 1.0, hits_max=100) is False
        assert ctx.blackboard["_retreat_strikes"] == 1

    def test_reset_clears_stale_dps_trajectory(self):
        """A stale HP sample from a prior fight must not feed the DPS estimate of
        the new one. After reset the first sample is treated as the baseline (no
        trajectory yet), so the trigger stays at the conservative baseline rather
        than mis-reading a huge HP gap as heavy incoming DPS."""
        from anima.procedures.combat_loop import BANDAGE_HP_PCT

        # Stale sample: a low HP% recorded long ago in the previous fight.
        ctx = _ctx(_bandage_hp_last=20.0, _bandage_hp_ts=1000.0)
        _reset_engagement_state(ctx)
        # New fight, first observation at full HP a "long time" later. Without
        # the reset this would compute a large (positive=recovery here, but the
        # gap is meaningless) trajectory off the stale sample; after reset it is
        # the baseline.
        assert _bandage_trigger_pct(ctx, 100.0, now=5000.0) == BANDAGE_HP_PCT
