"""Mid-run reagent exhaustion must still credit the Magery/Meditation gains
that already rolled.

practice_magery casts Greater Heal in a loop; each resolved cast rolls Magery
(fizzles included). When the backpack runs out of reagents PART-WAY through a
run the loop early-returns MISSING_RESOURCE. The bug: that early return dropped
``skill_gains`` entirely, so casts that genuinely raised Magery/Meditation
earlier in the same window were erased from the reward signal — a real partial
success reported as a pure failure (the phantom-success anti-pattern in
reverse, which the fitness backbone reads via skill_gains). The fix collects
the gains-so-far on the early-return path too.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_magery as pm
from anima.procedures.practice_magery import PracticeMagery


class _Skill:
    def __init__(self, value: float) -> None:
        self.value = value


def _ss(mana=50):
    mag = _Skill(35.0)
    med = _Skill(50.0)
    skills = {pm.SKILL_MAGERY: mag, pm.SKILL_MEDITATION: med}
    return SimpleNamespace(
        is_alive=True, serial=0x1, mana=mana,
        skills=SimpleNamespace(get=skills.get),
        _mag=mag, _med=med,
    )


@pytest.mark.asyncio
async def test_midrun_reagent_exhaustion_keeps_earned_gains(monkeypatch):
    # asyncio.sleep is awaited after every cast — mock it so the test is instant.
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())
    # Never need to meditate (start with full mana, draining slowly).
    monkeypatch.setattr(pm, "meditate", AsyncMock())

    ss = _ss(mana=50)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    calls = {"n": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        calls["n"] += 1
        # First two casts succeed and raise Magery; the third is out of reagents.
        if calls["n"] <= 2:
            ss._mag.value += 0.5  # real skill gain on a resolved cast
            return SimpleNamespace(success=True, fizzled=False, no_reagents=False)
        return SimpleNamespace(success=False, fizzled=False, no_reagents=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)

    result = await PracticeMagery().execute(ctx)

    # Early-returned on reagents...
    assert result.success is False
    assert result.reason == pm.FailureReason.MISSING_RESOURCE
    # ...but the two casts' worth of Magery gain (2 * 0.5) survives in the
    # reward signal instead of being silently dropped.
    assert result.skill_gains.get(pm.SKILL_MAGERY) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_reagent_exhaustion_with_no_prior_gain_reports_empty(monkeypatch):
    # If reagents run out on the VERY FIRST cast (no skill rolled yet), the
    # early return must still be a clean MISSING_RESOURCE with no phantom gains.
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(pm, "meditate", AsyncMock())

    ss = _ss(mana=50)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        return SimpleNamespace(success=False, fizzled=False, no_reagents=True)

    monkeypatch.setattr(pm, "cast_spell", fake_cast)

    result = await PracticeMagery().execute(ctx)

    assert result.success is False
    assert result.reason == pm.FailureReason.MISSING_RESOURCE
    assert result.skill_gains == {}
