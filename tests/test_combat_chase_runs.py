"""The melee combat loop must RUN — not walk — to close the gap on a target.

A target that has stepped out of melee range is almost always inside
``ENGAGE_RANGE`` (< 8 tiles). go_to's auto-run policy (``run=None``) walks any
leg shorter than ``RUN_MIN_REMAINING`` (8) at 400ms/step — the same or faster
cadence a kiting mob moves at — so the agent never re-closes melee range and
the swing never re-lands. Every other movement leg in this loop (retreat,
reposition, wander) already forces ``run=True``; the gap-closing chase was the
lone walker. This test pins the chase to ``run=True`` so it runs the target
down and keeps swings-per-engagement up.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby


def _mob(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(target):
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        stam=100, stam_max=100,
        equipment={1: 0xDEAD},          # weapon already in hand → no equip step
        skills={},                       # .get(...) → None → before/after = 0.0
    )

    mobiles = {target.serial: target}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(
        nearby_mobiles=nearby,
        mobiles=mobiles,
        items={},                        # no corpses to loot → clean no-op
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_chase_closes_the_gap_running(monkeypatch):
    # Target alive, 4 tiles away (inside ENGAGE_RANGE, below RUN_MIN_REMAINING=8
    # → auto-run would WALK). The chase must override that with run=True.
    target = _mob(0x2, 104, 100)
    ctx = _ctx(target)

    go_to_calls: list[dict] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_calls.append({"x": x, "y": y, "run": run})
        # As soon as the agent reaches the target, mark it dead so the loop
        # finds no further target and exits promptly.
        target.hits = 0
        target.is_dead = True
        c.perception.self_state.x, c.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    # No real shield in pack and no waiting on the tick clock.
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant_sleep)

    result = await HuntNearby().execute(ctx)

    # The chase leg ran; it was not left to walk.
    chase_legs = [c for c in go_to_calls if (c["x"], c["y"]) == (104, 100)]
    assert chase_legs, "the gap-closing chase go_to must have been invoked"
    assert all(c["run"] is True for c in chase_legs), (
        "combat chase must force run=True, not auto-walk a short in-range leg"
    )
    assert result.success is True
