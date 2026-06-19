"""A death mid-fight must break the hunt loop immediately.

``HuntNearby.can_start`` gates on ``is_alive`` but the engagement loop did not:
if the agent was *killed* mid-fight (hits → 0, ghost), the loop kept spinning up
to the full 45s ``ENGAGEMENT_CAP_S`` swinging as a corpse while the planner's
Priority-0 death branch (seek_resurrection) sat blocked behind the still-running
procedure. The low-HP retreat path can't cover this — it is HP-floor logic
debounced over ``RETREAT_CONFIRM_TICKS`` and routes to ``bandage_self`` (a ghost
can't bandage). Death is terminal: the loop must bail at once, skip the
anchor walk-back, and return a plain failure with no re-hunt suggestion so
control falls through to the planner's resurrection branch.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=hits, hits_max=hits_max,
    )


def _ctx(mobiles):
    # is_alive is a real property of hits/hits_max so a death (hits=0) flips it,
    # mirroring SelfState.is_alive (hits > 0 or hits_max == 0).
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

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items={})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_death_mid_fight_breaks_loop_immediately(monkeypatch):
    # One live hostile, adjacent so no chase is needed. On the first loop tick
    # the agent dies (hits → 0). The loop must exit on that tick, not run 45.
    foe = _mob(0x2, 101, 100)
    mobiles = {foe.serial: foe}
    ctx = _ctx(mobiles)

    go_to_calls: list[tuple] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_calls.append((x, y))
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    ticks = {"n": 0}

    async def _sleep_and_die(_):
        ticks["n"] += 1
        if ticks["n"] == 1:
            # The killing blow lands: hits hit 0, is_alive flips False.
            ctx.perception.self_state.hits = 0
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _sleep_and_die)

    result = await HuntNearby().execute(ctx)

    # Bailed on the very first tick — not spun the full ENGAGEMENT_CAP_S.
    assert ticks["n"] == 1, "loop must break on the tick the agent dies"
    # Death is a failure that yields to the planner's death branch: no success,
    # and crucially NO next_suggestion to chain a re-hunt or bandage off of
    # (a ghost can't do either — resurrection must take over).
    assert result.success is False
    assert result.next_suggestion is None
    assert "died" in result.message
    # A ghost must not be walked back to the arena anchor (the planner walks it
    # to a healer instead).
    assert go_to_calls == [], "no anchor walk-back while dead"


@pytest.mark.asyncio
async def test_survivor_still_suggests_rehunt(monkeypatch):
    # Control: an agent that stays alive through the cap exits normally and the
    # death path does NOT swallow the usual hunt_nearby re-suggestion.
    foe = _mob(0x2, 101, 100)
    mobiles = {foe.serial: foe}
    ctx = _ctx(mobiles)

    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    monkeypatch.setattr(cl, "ENGAGEMENT_CAP_S", 0.0)  # exit after one tick
    monkeypatch.setattr(cl.asyncio, "sleep", AsyncMock(return_value=None))

    result = await HuntNearby().execute(ctx)

    assert ctx.perception.self_state.is_alive is True
    assert result.next_suggestion == "hunt_nearby"
