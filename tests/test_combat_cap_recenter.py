"""The engagement-cap timeout must re-center the agent on the combat anchor.

The melee loop boxes each engagement at ``ENGAGEMENT_CAP_S`` (45s). When that
deadline elapses while the agent is mid-chase — the gap-closing chase having
walked it up to ``MAX_CHASE_FROM_ANCHOR`` tiles off the anchor — the loop exits
normally with ``retreated`` and ``leashed`` both False. The old code only walked
back to the anchor on retreat/leash, so a cap timeout stranded the agent
wherever the chase left it, drifting the combat anchor out from under
``WanderForCombat``'s orbit — exactly the "drift away from the anchor" the loop's
docstring and the leash promise it won't do.

This pins the fix: ANY disengage that leaves the agent off the anchor (here, the
45s cap firing mid-chase) must return it to the anchor. The control case (a
kill-fest that never moved off the anchor) must NOT emit a spurious walk-back.
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
async def test_disengage_offanchor_recenters(monkeypatch):
    # Target sits inside the leash but a step out of melee (dist>1) and never
    # dies — so the loop chases it, drifting the agent off the anchor, and never
    # exits on a kill. We end the engagement the way the engagement-cap timeout
    # would (a clean loop exit with retreated/leashed both False) by dropping the
    # connection after the first chase leg. The agent must then be walked back to
    # the anchor — the old code only re-centred on retreat/leash and stranded it.
    target = _mob(0x2, 109, 100)   # 9 east: in ENGAGE_RANGE, dist>1, inside leash
    ctx = _ctx(target)

    walked_to: list[tuple[int, int]] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        walked_to.append((x, y))
        c.perception.self_state.x, c.perception.self_state.y = x, y
        # First leg is the chase onto the target's tile (off the anchor). End the
        # loop here the same way a cap timeout would: a clean exit, no retreat,
        # no leash. The follow-up walk-back must still fire.
        if (x, y) == (target.x, target.y):
            c.conn.connected = False
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant_sleep)

    result = await HuntNearby().execute(ctx)

    # The chase dragged the agent off the anchor, and the loop exited with no
    # retreat/leash — yet the agent ends the procedure standing back on the anchor.
    assert walked_to, "the loop must have moved (chased) at least once"
    chase_off_anchor = any(dest != (100, 100) for dest in walked_to)
    assert chase_off_anchor, "test must exercise a chase that leaves the anchor"
    assert walked_to[-1] == (100, 100), (
        "after a disengage that left the agent off the anchor it must walk back, "
        f"but the last move was to {walked_to[-1]}"
    )
    assert (ctx.perception.self_state.x, ctx.perception.self_state.y) == (100, 100)
    # This was neither a leash break nor a retreat — the message stays clean.
    assert "leashed back to anchor" not in result.message
    assert "retreated" not in result.message


@pytest.mark.asyncio
async def test_killfest_on_anchor_does_not_walk_back(monkeypatch):
    # Control: target is adjacent (dist<=1, never chased). It is alive at engage
    # time (so the loop actually starts) and drops to 0 HP on the first tick. The
    # agent never leaves the anchor, so there must be NO spurious walk-back.
    target = _mob(0x2, 101, 100)   # adjacent → no chase leg
    ctx = _ctx(target)

    walked_to: list[tuple[int, int]] = []
    tick = {"n": 0}

    async def fake_sleep(_):
        # On the first in-loop sleep, fell the target so the next death check
        # ends the engagement with no chase and no next target.
        tick["n"] += 1
        if tick["n"] == 1:
            target.hits = 0
            target.is_dead = True
        return None

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        walked_to.append((x, y))
        c.perception.self_state.x, c.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    monkeypatch.setattr(cl.asyncio, "sleep", fake_sleep)

    result = await HuntNearby().execute(ctx)

    # Stayed on the anchor the whole time → no walk-back was emitted.
    assert (100, 100) not in walked_to, (
        "a kill-fest that never left the anchor must not emit a walk-back"
    )
    assert (ctx.perception.self_state.x, ctx.perception.self_state.y) == (100, 100)
    assert result.success is True
    # MAX_CHASE_FROM_ANCHOR is imported to keep the leash constant referenced
    # alongside the cap-recenter behaviour it complements.
    assert MAX_CHASE_FROM_ANCHOR > 0
