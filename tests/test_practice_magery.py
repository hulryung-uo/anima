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
