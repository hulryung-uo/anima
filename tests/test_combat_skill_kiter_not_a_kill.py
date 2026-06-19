"""MeleeAttack must not credit a kill when the target merely leaves view.

A mobile leaves ``world.mobiles`` both when it dies AND when it walks out of
visual range (0x1D Delete / stale prune). The skill loop previously treated any
disappearance as a kill — handing a +15 reward and ``success=True`` for a foe
that simply fled, polluting the Q-learning reward signal. This pins the fix:
a vanished target is only a kill when there is positive death evidence (a corpse
on the ground, or a last-known health bar at zero). asyncio.sleep and
time.monotonic are mocked so the test terminates instantly.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.skills.combat.melee as melee
from anima.perception.enums import NotorietyFlag
from anima.skills.combat.melee import MeleeAttack


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, hits=hits, hits_max=hits_max, name=None,
    )


def _ctx(mobiles, corpses=None):
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        last_damage_taken_at=0.0,
    )

    def nearby(x, y, distance=10):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    # find_corpses scans world.items for graphic 0x2006 within range.
    items = {}
    for i, (cx, cy) in enumerate(corpses or []):
        items[0xC000 + i] = SimpleNamespace(
            serial=0xC000 + i, graphic=0x2006, container=0, x=cx, y=cy,
        )

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items=items)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={"persona": SimpleNamespace(combat_disposition="aggressive")},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def _patch_clock(monkeypatch):
    # A monotonic clock that advances 0.5s per call so the COMBAT_TIMEOUT
    # deadline is reached after a couple of ticks (no real wall time spent).
    t = {"v": 0.0}

    def fake_monotonic():
        t["v"] += 0.5
        return t["v"]

    monkeypatch.setattr(melee.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(melee.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_kiter_leaving_view_is_not_a_kill(monkeypatch):
    # The target vanishes from world.mobiles with NO corpse and no zero-HP
    # reading — it fled. This must NOT be credited as a kill.
    foe = _mob(0x2, 101, 100, hits=50, hits_max=50)
    mobiles = {foe.serial: foe}
    ctx = _ctx(mobiles, corpses=[])
    _patch_clock(monkeypatch)

    real_sleep = melee.asyncio.sleep

    async def _sleep_then_vanish(_):
        # After the first tick (which records the foe's 50/50 health bar), the
        # foe walks out of view → dropped from world.mobiles, no corpse.
        mobiles.pop(foe.serial, None)
        return await real_sleep(_)

    monkeypatch.setattr(melee.asyncio, "sleep", _sleep_then_vanish)

    result = await MeleeAttack().execute(ctx)

    assert result.success is False, "a fled kiter must not count as a kill"
    assert result.reward < 0, "escape must not yield the kill reward"


@pytest.mark.asyncio
async def test_corpse_on_ground_confirms_the_kill(monkeypatch):
    # Same disappearance, but a corpse is on the ground nearby — positive death
    # evidence, so this IS a kill.
    foe = _mob(0x2, 101, 100, hits=50, hits_max=50)
    mobiles = {foe.serial: foe}
    ctx = _ctx(mobiles, corpses=[(101, 100)])
    _patch_clock(monkeypatch)

    real_sleep = melee.asyncio.sleep

    async def _sleep_then_die(_):
        mobiles.pop(foe.serial, None)
        return await real_sleep(_)

    monkeypatch.setattr(melee.asyncio, "sleep", _sleep_then_die)

    result = await MeleeAttack().execute(ctx)

    assert result.success is True, "a corpse on the ground confirms the kill"
    assert result.reward > 0
