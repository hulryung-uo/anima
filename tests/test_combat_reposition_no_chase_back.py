"""The anti-surround reposition must not be undone by the same-tick chase.

``_reposition_if_surrounded`` steps the agent ``REPOSITION_STEP`` (2) tiles away
from the hostile centroid when boxed in (>= ``SURROUND_COUNT`` adjacent), so that
for a beat fewer mobs are in melee range and an in-flight bandage can resolve.
By construction that step leaves the primary swing target at Chebyshev dist > 1.

Bug: the gap-closing chase later in the same loop iteration fires on ``dist > 1``
and walks the agent straight back onto the target's tile — re-entering the pile
and erasing the separation within the same tick. The fix skips the chase on any
tick we repositioned, so the separation actually holds for a swing cycle.

The whole engagement loop is driven here with ``asyncio.sleep`` and
``time.monotonic`` mocked so exactly one tick runs — the real 45s loop never
executes.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import SURROUND_COUNT, HuntNearby


def _mob(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(mobiles):
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        stam=100, stam_max=100,
        equipment={1: 0xDEAD},          # weapon already in hand → no equip step
        skills={},
    )
    mob_map = {m.serial: m for m in mobiles}

    def nearby(x, y, distance=18):
        return [m for m in mob_map.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mob_map, items={})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def _patch_one_tick(monkeypatch):
    """Make the engagement loop run exactly one tick, instantly.

    ``asyncio.sleep`` is a no-op; ``time.monotonic`` yields 0, 0, then a value
    past the deadline so the ``while`` condition exits after a single iteration.
    The per-tick heal helpers are stubbed to no-ops so they neither consume
    extra ``monotonic`` reads nor send packets that perturb the test.
    """
    async def _instant_sleep(_):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant_sleep)

    # Return 0.0 for the first two reads (deadline calc + the first while-check)
    # then a value past the deadline forever after, so EXACTLY one iteration runs
    # no matter how many monotonic reads the body makes (robust vs a fixed-length
    # iterator, which StopIteration'd when the loop read monotonic >3 times).
    _mstate = {"n": 0}

    def _mono() -> float:
        _mstate["n"] += 1
        return 0.0 if _mstate["n"] <= 2 else cl.ENGAGEMENT_CAP_S + 100.0

    monkeypatch.setattr(cl.time, "monotonic", _mono)

    async def _noop(_ctx):
        return False

    monkeypatch.setattr(cl, "_maybe_quaff_heal_potion", _noop)
    monkeypatch.setattr(cl, "_maybe_bandage", AsyncMock(return_value=None))
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))


@pytest.mark.asyncio
async def test_chase_does_not_undo_the_reposition(monkeypatch):
    # Three hostiles boxed in tight on the agent at (100,100): the primary
    # target adjacent to the east plus two more adjacent → surrounded.
    target = _mob(0x2, 101, 100)
    pack = [target, _mob(0x3, 100, 101), _mob(0x4, 101, 101)]
    ctx = _ctx(pack)

    go_to_dests: list[tuple[int, int]] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_dests.append((x, y))
        # Model the reposition: actually step the agent to the dest tile, so
        # afterwards the target sits at dist>1 (the condition that would trip
        # the buggy chase).
        c.perception.self_state.x, c.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    _patch_one_tick(monkeypatch)

    # Sanity: the agent really is surrounded at the start of the tick.
    assert len(cl._adjacent_hostiles(ctx)) >= SURROUND_COUNT

    await HuntNearby().execute(ctx)

    # Exactly one reposition step happened (away from the SE pack → NW), and the
    # chase did NOT then walk back onto the target's tile (101,100). With the bug
    # the chase fires in the same tick and (101,100) appears in go_to_dests.
    assert go_to_dests, "the reposition should have stepped the agent"
    reposition_dest = go_to_dests[0]
    assert reposition_dest[0] < 100 and reposition_dest[1] < 100  # backed off NW
    assert (target.x, target.y) not in go_to_dests, (
        "the gap-closing chase re-walked onto the target's tile in the same "
        "tick it repositioned — undoing the anti-surround step"
    )


@pytest.mark.asyncio
async def test_chase_still_runs_when_not_repositioned(monkeypatch):
    # Control: a single distant target (no surround) must still be chased — the
    # fix must only suppress the chase on a reposition tick, not in general.
    target = _mob(0x2, 105, 100)   # 5 tiles east, dist>1, not adjacent
    ctx = _ctx([target])

    go_to_dests: list[tuple[int, int]] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_dests.append((x, y))
        c.perception.self_state.x, c.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    _patch_one_tick(monkeypatch)

    assert len(cl._adjacent_hostiles(ctx)) < SURROUND_COUNT  # not surrounded

    await HuntNearby().execute(ctx)

    # No reposition (not surrounded), so the chase must have closed onto the
    # target's tile this tick.
    assert (105, 100) in go_to_dests, "an un-surrounded distant target must be chased"
