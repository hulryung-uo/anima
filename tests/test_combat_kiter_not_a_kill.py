"""A target that LEAVES VISUAL RANGE must not be mis-credited as a kill.

A mobile is dropped from ``world.mobiles`` (``current is None`` in the hunt
loop) by a 0x1D Delete *and* by ``prune_stale_mobiles`` — but UO fires a 0x1D
Delete whenever a mobile leaves the player's view, not only when it dies, and a
stale-update prune likewise reaps a mob that simply pathed away. So
``current is None`` is NOT proof of death.

The old loop counted every disappearance as a kill: a fast kiter that ran out of
view (or one the chase leash let go) was recorded as a win, inflating the COMBAT
throughput metric the loop's fitness reads and firing a phantom
``_loot_fresh_corpses`` pass over an empty floor. ``_confirm_kill`` now gates the
no-current case on real evidence — a last-seen zero health bar, or a corpse on
the ground — so only a genuine kill is credited.

asyncio.sleep and time.monotonic are mocked so the engagement loop terminates
instantly (no real timing).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby, _confirm_kill


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=hits, hits_max=hits_max,
    )


def _corpse(serial, x, y):
    # find_corpses keys off container == 0 and graphic == CORPSE_GRAPHIC.
    return SimpleNamespace(serial=serial, x=x, y=y, container=0, graphic=0x2006)


def _ctx(mobiles, items=None):
    class _SS:
        x = 100
        y = 100
        serial = 0x1
        hits = 100
        hits_max = 100
        stam = 100
        stam_max = 100
        equipment = {1: 0xDEAD}
        skills: dict = {}

        @property
        def hp_percent(self):
            return (self.hits / self.hits_max) * 100.0 if self.hits_max else 100.0

        @property
        def is_alive(self):
            return self.hits > 0 or self.hits_max == 0

    ss = _SS()

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(
        nearby_mobiles=nearby, mobiles=mobiles,
        items={} if items is None else items,
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def _freeze_clock(monkeypatch):
    """Make time.monotonic advance one engagement-cap per tick so the loop ends.

    Each asyncio.sleep call bumps a counter; time.monotonic returns it scaled so
    the deadline (set to now + ENGAGEMENT_CAP_S) is crossed after the controlled
    number of ticks, never the wall clock.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(cl.time, "monotonic", lambda: clock["t"])
    return clock


def test_confirm_kill_requires_evidence():
    # Healthy last reading + no corpse -> left view, NOT a kill.
    ctx = _ctx({}, items={})
    assert _confirm_kill(ctx, last_hits=50, last_hits_max=50) is False
    # Last-seen health bar at zero -> watched it die -> kill.
    assert _confirm_kill(ctx, last_hits=0, last_hits_max=50) is True
    # No known health bar but a corpse on the ground -> kill.
    ctx2 = _ctx({}, items={0xC0: _corpse(0xC0, 101, 100)})
    assert _confirm_kill(ctx2, last_hits=0, last_hits_max=0) is True


@pytest.mark.asyncio
async def test_kiter_leaving_view_is_not_a_kill(monkeypatch):
    # One adjacent live hostile, queried so its health bar is known and full.
    # On the second tick it vanishes from world.mobiles (left view / pruned)
    # with NO corpse spawned — this must NOT be credited as a kill.
    foe = _mob(0x2, 101, 100, hits=50, hits_max=50)
    mobiles = {foe.serial: foe}
    ctx = _ctx(mobiles, items={})

    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    loot_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(cl, "_loot_fresh_corpses", loot_mock)

    clock = _freeze_clock(monkeypatch)

    ticks = {"n": 0}

    async def _sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            # The kiter runs out of view: the server 0x1D-Deletes it, so it
            # leaves world.mobiles. No corpse appears (it didn't die).
            mobiles.pop(foe.serial, None)
        # Advance a fraction of the cap per tick: several ticks fit before the
        # deadline so tick 2 (the disappearance) runs, then the loop ends.
        clock["t"] += cl.ENGAGEMENT_CAP_S / 5.0
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _sleep)

    result = await HuntNearby().execute(ctx)

    # The disappearance must NOT have been credited as a kill, and the phantom
    # loot pass must never have run.
    assert "0 kills" in result.message, result.message
    loot_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_kill_with_corpse_is_credited(monkeypatch):
    # Same setup, but when the foe vanishes a corpse is on the ground — that is
    # positive death evidence, so it IS counted as a kill and looted.
    foe = _mob(0x2, 101, 100, hits=50, hits_max=50)
    mobiles = {foe.serial: foe}
    items: dict = {}
    ctx = _ctx(mobiles, items=items)

    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    loot_mock = AsyncMock(return_value=7)
    monkeypatch.setattr(cl, "_loot_fresh_corpses", loot_mock)

    clock = _freeze_clock(monkeypatch)

    ticks = {"n": 0}

    async def _sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            # The killing blow lands: foe gone from world.mobiles, corpse spawns
            # where it stood (adjacent to the agent).
            mobiles.pop(foe.serial, None)
            items[0xC0] = _corpse(0xC0, 101, 100)
        # Advance a fraction of the cap per tick: several ticks fit before the
        # deadline so tick 2 (the disappearance) runs, then the loop ends.
        clock["t"] += cl.ENGAGEMENT_CAP_S / 5.0
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _sleep)

    result = await HuntNearby().execute(ctx)

    assert "1 kills" in result.message, result.message
    loot_mock.assert_awaited()
