"""A disrupted cast must NOT consume a guaranteed Magery cast slot.

cast_spell reports ``disrupted=True`` when damage interrupts the cast before
the target cursor arrives: the spell is ruined, NO Magery CheckSkill rolled,
and the mana is NOT spent (the CastResult docstring says "the caller should
just recast"). The g_00101 champion loop counts only RESOLVED casts so a
low-mana *meditation* never burns one of the CASTS_PER_RUN slots — but the
equally roll-less *disrupted* case used to ``casts_done += 1`` anyway, silently
under-rolling the MAGIC loop for any combat-adjacent (warrior-mage) seed.

Pins: under steady disruption the loop recasts (bounded) and still resolves the
full CASTS_PER_RUN real Magery checks instead of finishing after CASTS_PER_RUN
*attempts* with a fraction of them disrupted.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_magery as pm
from anima.procedures.practice_magery import CASTS_PER_RUN, PracticeMagery


def _ss(mana=50, mana_max=50):
    mag = SimpleNamespace(value=35.0)
    med = SimpleNamespace(value=50.0)
    skills = {pm.SKILL_MAGERY: mag, pm.SKILL_MEDITATION: med}
    return SimpleNamespace(
        is_alive=True, serial=0x1, mana=mana, mana_max=mana_max,
        skills=SimpleNamespace(get=skills.get),
    )


@pytest.mark.asyncio
async def test_disrupted_cast_does_not_consume_a_slot(monkeypatch):
    """Every other cast is disrupted; the loop must still resolve CASTS_PER_RUN
    *successful* casts (disruptions don't eat a slot)."""
    ss = _ss(mana=50)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    calls = {"n": 0, "resolved": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        calls["n"] += 1
        # Alternate: odd attempts are disrupted (no roll, mana intact),
        # even attempts resolve and drain mana.
        if calls["n"] % 2 == 1:
            return SimpleNamespace(
                success=False, fizzled=False, no_reagents=False,
                no_mana=False, disrupted=True,
            )
        calls["resolved"] += 1
        # plenty of mana so meditation never interferes
        return SimpleNamespace(
            success=True, fizzled=False, no_reagents=False,
            no_mana=False, disrupted=False,
        )

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", AsyncMock())
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    result = await PracticeMagery().execute(ctx)

    # The disruptions cost zero cast slots → exactly CASTS_PER_RUN resolved.
    assert calls["resolved"] == CASTS_PER_RUN, (
        f"disrupted casts must not consume a guaranteed slot; "
        f"resolved {calls['resolved']} of {CASTS_PER_RUN}"
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_perpetual_disruption_is_bounded(monkeypatch):
    """If EVERY cast is disrupted the loop must not spin forever — it bails out
    via the disruption guard and books a zero-cast BLOCKED failure."""
    ss = _ss(mana=50)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss))

    calls = {"n": 0}

    async def fake_cast(_ctx, _spell, target_serial=None, mana_cost=0):
        calls["n"] += 1
        return SimpleNamespace(
            success=False, fizzled=False, no_reagents=False,
            no_mana=False, disrupted=True,
        )

    monkeypatch.setattr(pm, "cast_spell", fake_cast)
    monkeypatch.setattr(pm, "meditate", AsyncMock())
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())

    from anima.procedures.base import FailureReason
    result = await PracticeMagery().execute(ctx)

    # Bounded: the loop stops; it never resolves a cast → BLOCKED, not success.
    assert result.success is False
    assert result.reason == FailureReason.BLOCKED
    # Guard cap is CASTS_PER_RUN disruptions — must not run unboundedly.
    assert calls["n"] <= CASTS_PER_RUN + 1
