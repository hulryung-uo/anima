"""The melee chase must be LEASHED to the combat anchor.

The combat anchor (set at engagement start) is the centre of the bounded arena
this loop is meant to fight in. The gap-closing chase, left unleashed, follows a
target wherever it goes — bounded only by the HP retreat interrupt and the 45s
cap. A fast/kiting mob that stays just out of melee yet inside ``ENGAGE_RANGE``
therefore drags the agent unboundedly far from the anchor: every tick the chase
re-targets the mob's new tile, the mob steps again, and the agent is walked clean
out of the arena chasing one kiter — exactly the "drift away from the anchor" the
loop's docstring and ``WanderForCombat`` promise it won't do.

This pins the leash: once a chased target's tile is more than
``MAX_CHASE_FROM_ANCHOR`` tiles (Chebyshev) from the anchor, the chase is NOT
extended; the engagement breaks and the agent returns to the anchor to re-center.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import MAX_CHASE_FROM_ANCHOR, HuntNearby


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
        skills={},
    )
    mobiles = {target.serial: target}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items={})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_chase_is_leashed_to_the_anchor(monkeypatch):
    # Agent and anchor at (100,100). Target starts 8 tiles east — inside
    # ENGAGE_RANGE (pickable) and dist>1 (a chase fires). Each chase the mob
    # kites another 8 tiles east, just staying ahead of melee. Left unleashed
    # the agent would follow it forever; the leash must cut the chase the moment
    # the target's tile crosses MAX_CHASE_FROM_ANCHOR from (100,100).
    target = _mob(0x2, 108, 100)
    ctx = _ctx(target)

    chase_legs: list[tuple[int, int]] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        # A chase leg: walk the agent onto the target's tile, then the kiter
        # steps another 8 east before the next tick re-reads its position.
        c.perception.self_state.x, c.perception.self_state.y = x, y
        if (x, y) == (target.x, target.y):
            chase_legs.append((x, y))
            target.x += 8
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant_sleep)

    result = await HuntNearby().execute(ctx)

    # Every chase leg the agent actually committed to must have landed on a tile
    # within the leash of the anchor — the loop never chased past the boundary.
    assert chase_legs, "the test must exercise at least one chase leg"
    for cx, cy in chase_legs:
        from_anchor = max(abs(cx - 100), abs(cy - 100))
        assert from_anchor <= MAX_CHASE_FROM_ANCHOR, (
            f"chased to ({cx},{cy}), {from_anchor} tiles from the anchor — "
            f"past the {MAX_CHASE_FROM_ANCHOR}-tile leash"
        )

    # The leash break re-centres the agent on the anchor and reports it.
    assert (ctx.perception.self_state.x, ctx.perception.self_state.y) == (100, 100)
    assert "leashed back to anchor" in result.message
    assert result.success is True              # healthy re-center, not a failure
    assert result.next_suggestion == "hunt_nearby"  # keep hunting, not bandage


@pytest.mark.asyncio
async def test_in_arena_chase_is_not_leashed(monkeypatch):
    # Control: a target that stays inside the leash is chased and killed
    # normally — the leash must not break an in-bounds engagement.
    target = _mob(0x2, 105, 100)   # 5 from anchor, well inside the leash
    ctx = _ctx(target)

    chase_legs: list[tuple[int, int]] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        chase_legs.append((x, y))
        c.perception.self_state.x, c.perception.self_state.y = x, y
        target.hits = 0
        target.is_dead = True          # reached it → dead → loop exits
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant_sleep)

    result = await HuntNearby().execute(ctx)

    assert (105, 100) in chase_legs, "in-arena target must be chased normally"
    assert "leashed back to anchor" not in result.message
    assert result.success is True
