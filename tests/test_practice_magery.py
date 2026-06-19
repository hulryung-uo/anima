"""Regression guard for the g_00101 magery champion (fitness 233.8).

The on-disk practice_magery once regressed from the champion (while-loop, 10
casts @ 0.5s, meditation-doesn't-consume-a-cast-slot) back to a 6-cast/4.0s
for-loop (~153) — and because force_seed plants HEAD-rooted genomes, every
HEAD-seeded run silently inherited the inferior version. These tests pin the
champion's two load-bearing properties so it can't regress unnoticed:

  1. throughput constants (>= 10 casts, fast cadence)
  2. a low-mana meditation does NOT consume a cast slot — the loop still
     resolves CASTS_PER_RUN real casts (the ~+25% Magery-checks/run win).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_magery as pm
from anima.procedures.practice_magery import (
    CASTS_PER_RUN,
    CAST_INTERVAL_S,
    PracticeMagery,
)


def test_cast_gate_allows_exact_cost_mana():
    # The cast gate must let a pool that regens to exactly the spell cost
    # cast — an inflated +1 floor strands low-Int mages (mana plateaus at the
    # cost but one short of the floor) at ZERO casts/run.
    assert pm.MEDITATE_BELOW_MANA <= pm.GREATER_HEAL_MANA


def test_throughput_constants():
    # The champion runs 10 casts at a tight cadence; the regressed baseline
    # was 6 @ 4.0s. Guard the floor.
    assert CASTS_PER_RUN >= 10
    assert CAST_INTERVAL_S <= 1.0


def test_loop_counts_only_resolved_casts_not_meditations():
    """Source-shape guard: the loop must count resolved casts (while +
    casts_done), never a bare `for _ in range(CASTS_PER_RUN)` that lets a
    low-mana meditation eat a cast slot."""
    import inspect
    src = inspect.getsource(PracticeMagery.execute)
    assert "while casts_done < CASTS_PER_RUN" in src
    assert "casts_done += 1" in src
    assert "for _ in range(CASTS_PER_RUN)" not in src


def _ss(mana=50):
    mag = SimpleNamespace(value=35.0)
    med = SimpleNamespace(value=50.0)
    skills = {pm.SKILL_MAGERY: mag, pm.SKILL_MEDITATION: med}
    return SimpleNamespace(
        is_alive=True, serial=0x1, mana=mana,
        skills=SimpleNamespace(get=skills.get),
    )


@pytest.mark.asyncio
async def test_meditation_does_not_consume_a_cast_slot(monkeypatch):
    """With mana low at the start, the loop meditates (restoring mana) and
    then still resolves the full CASTS_PER_RUN casts — the meditation did not
    burn a slot. This is the load-bearing property of the g_00101 champion."""
    ss = _ss(mana=0)  # start out of mana → first iteration meditates
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    cast_calls = {"n": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        cast_calls["n"] += 1
        ss.mana -= 5  # casting drains a little
        return SimpleNamespace(success=True, fizzled=False, no_reagents=False)

    async def fake_meditate(_ctx, target_pct=0.0, timeout=0.0):
        ss.mana = 50  # meditation restores mana
        return SimpleNamespace(success=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", fake_meditate)
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    result = await PracticeMagery().execute(ctx)

    # The meditation(s) cost zero cast slots → exactly CASTS_PER_RUN resolved.
    assert cast_calls["n"] == CASTS_PER_RUN
    assert result.success is True


@pytest.mark.asyncio
async def test_out_of_reagents_returns_missing_resource(monkeypatch):
    ss = _ss(mana=50)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        return SimpleNamespace(success=False, fizzled=False, no_reagents=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", AsyncMock())
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    from anima.procedures.base import FailureReason
    result = await PracticeMagery().execute(ctx)
    assert result.success is False
    assert result.reason == FailureReason.MISSING_RESOURCE


@pytest.mark.asyncio
async def test_exact_cost_mana_pool_still_grinds(monkeypatch):
    """Regression: a small mana pool that regenerates to exactly the spell
    cost must keep casting, not stall at zero.

    Models a low-Int starter whose meditation plateaus at GREATER_HEAL_MANA
    (e.g. mana_max=18, 60% target ≈ 11). Each cast drains the pool back below
    the floor, so every iteration: meditate up to the cost → cast → repeat.
    With the old +1 gate the cast never fired (11 < 12) and the stall guard
    tripped after 2 meditations → 0 casts. The corrected gate resolves the
    full CASTS_PER_RUN.
    """
    cost = pm.GREATER_HEAL_MANA
    ss = _ss(mana=0)  # start out of mana → first iteration meditates
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    cast_calls = {"n": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        cast_calls["n"] += 1
        ss.mana = 0  # casting drains the small pool back to empty
        return SimpleNamespace(success=True, fizzled=False, no_reagents=False)

    async def fake_meditate(_ctx, target_pct=0.0, timeout=0.0):
        # Small pool: meditation only ever reaches the exact spell cost —
        # never the old +1 floor.
        ss.mana = cost
        return SimpleNamespace(success=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", fake_meditate)
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    result = await PracticeMagery().execute(ctx)

    assert cast_calls["n"] == CASTS_PER_RUN
    assert result.success is True


@pytest.mark.asyncio
async def test_can_start_false_when_reagents_short(monkeypatch):
    """The precondition fails fast on a reagent shortage instead of relying on
    the reactive 'More reagents are needed' journal line after a wasted cast.

    Greater Heal needs Ginseng+Garlic+Mandrake+Sulfurous Ash; the backpack here
    holds only Ginseng, so can_start must be False (spellbook present)."""
    from anima.core.spells import REAGENT_GRAPHICS

    ginseng = REAGENT_GRAPHICS["Ginseng"]
    spellbook = SimpleNamespace(graphic=0x0EFA, container=0x99)
    one_ginseng = SimpleNamespace(graphic=ginseng, container=0x99, amount=5)
    world = SimpleNamespace(items={0xA: one_ginseng})
    ss = SimpleNamespace(
        is_alive=True,
        equipment={0x15: 0x99},  # backpack serial
    )
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world)
    )
    # Spellbook present (equipped) but reagents short → can_start is False.
    monkeypatch.setattr(PracticeMagery, "_has_spellbook", lambda self, _c: True)

    assert await PracticeMagery().can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_true_with_full_reagent_kit(monkeypatch):
    from anima.core.spells import REAGENT_GRAPHICS

    gl = REAGENT_GRAPHICS["Ginseng"]
    gs = REAGENT_GRAPHICS["Garlic"]
    mr = REAGENT_GRAPHICS["Mandrake Root"]
    sa = REAGENT_GRAPHICS["Sulfurous Ash"]
    items = {
        i: SimpleNamespace(graphic=g, container=0x99, amount=3)
        for i, g in enumerate((gl, gs, mr, sa))
    }
    world = SimpleNamespace(items=items)
    ss = SimpleNamespace(is_alive=True, equipment={0x15: 0x99})
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))
    monkeypatch.setattr(PracticeMagery, "_has_spellbook", lambda self, _c: True)

    assert await PracticeMagery().can_start(ctx) is True


@pytest.mark.asyncio
async def test_meditation_stall_does_not_spin_forever(monkeypatch):
    """Low mana pool: meditation can never lift mana above the cast threshold
    (MEDITATE_BELOW_MANA). The loop must NOT spin forever meditating — it must
    bail out after a bounded number of fruitless meditations instead of
    burning the whole procedure window with zero casts."""
    ss = _ss(mana=0)  # permanently below threshold; meditation never helps
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    cast_calls = {"n": 0}
    med_calls = {"n": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        cast_calls["n"] += 1
        return SimpleNamespace(success=True, fizzled=False, no_reagents=False)

    async def fake_meditate(_ctx, target_pct=0.0, timeout=0.0):
        med_calls["n"] += 1
        # mana stays at 0 — meditation can't clear MEDITATE_BELOW_MANA
        return SimpleNamespace(success=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", fake_meditate)
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    # Bound the whole call so a regression (infinite loop) fails loudly
    # instead of hanging the suite.
    result = await asyncio.wait_for(PracticeMagery().execute(ctx), timeout=5.0)

    # No cast ever became possible, and the loop bailed after a bounded number
    # of meditations rather than spinning forever.
    assert cast_calls["n"] == 0
    assert med_calls["n"] <= 3
    assert result.success is False
