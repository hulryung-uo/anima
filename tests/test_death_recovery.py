"""Backlog rank 6: combat death-recovery floor (flee / recover-own-corpse /
re-equip). Warriors used to heal in place when surrounded (melee cancels the
bandage → death) and revive naked next to an un-looted corpse → re-death.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.actions.loot as loot
import anima.actions.equip as equip
import anima.planner.planner as P
from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    _FleeFromHostiles,
    _RecoverAfterDeath,
    _count_hostiles,
)


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety)


def _ctx(self_x=100, self_y=100, mobiles=(), items=None, armed=False, alive=True):
    ss = SimpleNamespace(
        is_alive=alive, x=self_x, y=self_y, serial=0x1,
        hits=10, hits_max=100,
        equipment={1: 0x999} if armed else {},
    )
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=0: [m for m in mobiles
                                                 if max(abs(m.x - x), abs(m.y - y)) <= distance],
        items=items or {},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
    )


class TestCountHostiles:
    def test_counts_attackable_excludes_self_and_far(self):
        ctx = _ctx(mobiles=[
            _mob(0x2, 103, 100),                                   # close enemy
            _mob(0x3, 101, 101),                                   # close enemy
            _mob(0x1, 100, 100),                                   # self (excluded)
            _mob(0x4, 100, 100, notoriety=NotorietyFlag.INNOCENT), # not attackable
            _mob(0x5, 130, 130),                                   # too far (dist 6)
        ])
        assert _count_hostiles(ctx, ctx.perception.self_state, dist=6) == 2


class TestFlee:
    @pytest.mark.asyncio
    async def test_flees_away_from_centroid(self, monkeypatch):
        dest = {}

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            dest["x"], dest["y"] = x, y

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        # hostiles to the EAST (x>100) → agent should flee WEST (x<100)
        ctx = _ctx(mobiles=[_mob(0x2, 108, 100), _mob(0x3, 110, 102), _mob(0x4, 109, 98)])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        assert result.next_suggestion == "bandage_self"
        assert dest["x"] < 100  # moved away from the eastern swarm

    @pytest.mark.asyncio
    async def test_no_hostiles_is_noop(self, monkeypatch):
        monkeypatch.setattr(movement, "go_to", AsyncMock())
        ctx = _ctx(mobiles=[])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True


class TestRecoverAfterDeath:
    @pytest.mark.asyncio
    async def test_can_start_true_with_corpse_or_weaponless(self, monkeypatch):
        monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [_mob(0x900, 100, 100)])
        ctx = _ctx(armed=True)  # armed, but a corpse is nearby
        assert await _RecoverAfterDeath().can_start(ctx) is True

        monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [])
        ctx2 = _ctx(armed=False)  # no corpse, but weaponless
        assert await _RecoverAfterDeath().can_start(ctx2) is True

        ctx3 = _ctx(armed=True)  # armed, no corpse → nothing to do
        assert await _RecoverAfterDeath().can_start(ctx3) is False

    @pytest.mark.asyncio
    async def test_can_start_false_when_dead(self, monkeypatch):
        monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [_mob(0x900, 100, 100)])
        ctx = _ctx(alive=False)
        assert await _RecoverAfterDeath().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_run_loots_reequips_and_clears_flag(self, monkeypatch):
        corpse = SimpleNamespace(serial=0x900, x=101, y=100)
        monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [corpse])
        monkeypatch.setattr(loot, "loot_corpse",
                            AsyncMock(return_value=SimpleNamespace(data={"items": 3})))
        monkeypatch.setattr(movement, "go_to", AsyncMock())
        monkeypatch.setattr(equip, "equip_weapon_from_pack",
                            AsyncMock(return_value=SimpleNamespace(success=True)))
        ctx = _ctx(armed=False)
        ctx.blackboard["_was_dead"] = True
        result = await _RecoverAfterDeath().run(ctx)
        assert result.success is True
        assert "re-equipped=True" in result.message
        assert ctx.blackboard["_was_dead"] is False  # bounded: one pass, flag cleared
