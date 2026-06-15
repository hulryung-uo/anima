"""Tests for WanderForCombat — the combat re-engage step that stops a weaponed
warrior from idling into the mining loop when no target is in range.

can_start must be the exact complement of HuntNearby on the target condition:
armed + a target in range → hunt handles it (wander stays off); armed + NO
target → wander roams to find one (instead of mining fallthrough).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import WanderForCombat
from anima.perception.enums import NotorietyFlag


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, body=0x0021):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety, body=body)


def _ctx(self_x=100, self_y=100, mobiles=(), armed=True, hp_pct=100.0):
    ss = SimpleNamespace(
        is_alive=True, hits_max=100, hp_percent=hp_pct,
        x=self_x, y=self_y, serial=0x1,
        equipment={1: 0x999} if armed else {},
    )
    world = SimpleNamespace(nearby_mobiles=lambda x, y, distance=0: list(mobiles))
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
    )


class TestCanStart:
    @pytest.mark.asyncio
    async def test_armed_no_target_activates(self):
        ctx = _ctx(mobiles=[])
        assert await WanderForCombat().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_armed_with_target_in_range_stays_off(self):
        # hunt_nearby owns this case; wander must NOT fire.
        ctx = _ctx(mobiles=[_mob(0x2, 102, 101)])  # within ENGAGE_RANGE=10
        assert await WanderForCombat().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_unarmed_stays_off(self, monkeypatch):
        monkeypatch.setattr(cl, "find_in_backpack", lambda ctx, g: [])
        ctx = _ctx(mobiles=[], armed=False)
        assert await WanderForCombat().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_low_hp_stays_off(self):
        ctx = _ctx(mobiles=[], hp_pct=20.0)
        assert await WanderForCombat().can_start(ctx) is False


class TestExecute:
    @pytest.mark.asyncio
    async def test_hands_back_to_hunt_when_target_spotted(self, monkeypatch):
        # go_to "finds" a target mid-walk: invoke interrupt_check after a mob
        # appears in the world.
        appeared = [_mob(0x2, 105, 100)]

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            # mob is now in range; the interrupt should detect it
            if interrupt_check:
                interrupt_check()

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=appeared)
        # can_start would be False now (target present), but execute is called
        # right after the walk; simulate: no target at start, target after walk.
        ctx.perception.world.nearby_mobiles = lambda x, y, distance=0: list(appeared)
        result = await WanderForCombat().execute(ctx)
        assert result.success is True
        assert result.next_suggestion == "hunt_nearby"

    @pytest.mark.asyncio
    async def test_keeps_wandering_when_still_empty(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=[])
        result = await WanderForCombat().execute(ctx)
        assert result.success is True                       # liveness, no fallthrough
        assert result.next_suggestion == "wander_for_combat"

    @pytest.mark.asyncio
    async def test_rotates_direction_each_run(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None
        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=[])
        proc = WanderForCombat()
        await proc.execute(ctx)
        assert ctx.blackboard["_wander_dir_idx"] == 1
        await proc.execute(ctx)
        assert ctx.blackboard["_wander_dir_idx"] == 2
