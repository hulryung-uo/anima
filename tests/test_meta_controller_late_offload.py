"""Late-phase economy/tidy offload.

Regression guard for the day-phase economy gate (HeuristicModePolicy branch e).
The mid phase offloads a heavy-but-not-overweight haul (weight >= 0.75) to
convert it to gold. The late phase is "socialize/tidy up" — tidying the haul is
part of that routine — but the offload gate used to be `phase == "mid"` ONLY,
so a late-phase avatar carrying a near-full pack drifted straight to socialize
and never converted the haul (branch b only catches >= 0.9 overweight). The
gate now fires in BOTH mid and late phases.
"""

from __future__ import annotations

import pytest

from anima.planner.meta_controller import HeuristicModePolicy, LivingState
from anima.planner.modes import MODES


def _make_state(**kw) -> LivingState:
    base = dict(
        hp_frac=0.9, weight_frac=0.2, gold=100, gold_rate_per_min=2.0,
        nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
        session_minutes=60.0, phase="late", last_modes=[], actual_mode="mining",
    )
    base.update(kw)
    return LivingState(**base)


@pytest.mark.asyncio
async def test_late_phase_heavy_pack_offloads_before_socializing():
    # Late phase, heavy-but-not-overweight pack, healthy positive gold rate so
    # the economy branch (c) does NOT fire — the day-phase tidy-up offload must.
    d = await HeuristicModePolicy().choose(
        _make_state(weight_frac=0.8, gold_rate_per_min=3.0,
                    last_modes=["mining", "combat"]))
    assert d.mode == "travel"
    assert d.mode in MODES
    assert d.goal is None
    assert "economy" in d.rationale
    assert "late" in d.rationale


@pytest.mark.asyncio
async def test_late_phase_light_pack_still_socializes():
    # A light pack has nothing to tidy → the late day-phase default (socialize)
    # is preserved. Guards against the fix over-reaching.
    d = await HeuristicModePolicy().choose(
        _make_state(weight_frac=0.2, gold_rate_per_min=3.0,
                    last_modes=["mining", "combat"]))
    assert d.mode == "socialize"
    assert d.goal is None


@pytest.mark.asyncio
async def test_mid_phase_heavy_pack_still_offloads():
    # The pre-existing mid-phase economy offload is unchanged by broadening
    # the gate to include the late phase.
    d = await HeuristicModePolicy().choose(
        _make_state(phase="mid", weight_frac=0.8, gold_rate_per_min=3.0,
                    last_modes=["mining", "combat"]))
    assert d.mode == "travel"
    assert "mid" in d.rationale


@pytest.mark.asyncio
async def test_early_phase_heavy_pack_does_not_offload():
    # Economy/tidy is a mid+late concern; an early-phase heavy pack keeps
    # grinding (the gate must not fire in the early phase).
    d = await HeuristicModePolicy().choose(
        _make_state(phase="early", session_minutes=10.0, weight_frac=0.8,
                    gold_rate_per_min=3.0, last_modes=["mining", "combat"]))
    assert d.mode == "mining"
    assert d.goal is None
