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


class TestDeathArmsPostResRecovery:
    """Wiring regression: when the agent is dead, the planner's Priority 0
    branch (the one that actually fires) must arm `_was_dead`. Otherwise the
    whole post-resurrection corpse-recovery / re-equip path is dead code and
    the agent revives naked → immediate re-death.
    """

    def _planner_and_dead_ctx(self):
        from anima.procedures.base import ProcedureRegistry
        from anima.planner.planner import Planner

        planner = Planner(ProcedureRegistry())
        ss = SimpleNamespace(
            is_alive=False, hits=0, hits_max=100, x=100, y=100, serial=0x1,
            equipment={},
        )
        ctx = SimpleNamespace(
            perception=SimpleNamespace(self_state=ss, world=None),
            blackboard={},
            bus=None,
        )
        return planner, ctx

    @pytest.mark.asyncio
    async def test_dead_arms_was_dead_and_seeks_resurrection(self):
        planner, ctx = self._planner_and_dead_ctx()
        proc = await planner.select_procedure(ctx)
        # Priority 0 must route to resurrection...
        assert getattr(proc, "name", None) == "seek_resurrection"
        # ...and arm the post-res recovery flag in the branch that fires.
        assert ctx.blackboard.get("_was_dead") is True


class TestSeekResurrectionCounterClearedOnRevive:
    """A resurrection that happens out of band (wandering healer / another
    player / a gump answered during the _DeathEscalate forum wait) brings the
    agent back alive WITHOUT seek_resurrection's own success tick — so its
    consecutive-fail counter is never zeroed. Left stale, the NEXT death
    escalates to the forum after far fewer than DEATH_ESCALATE_THRESHOLD
    failures. The dead→alive transition (Priority 1a) must clear it.
    """

    def _alive_ctx(self):
        # Alive, backpack equipped + a weapon so _RecoverAfterDeath.can_start
        # is False unless a corpse is present; empty world so the inventory
        # snapshot reads clean and we reach Priority 1a.
        ss = SimpleNamespace(
            is_alive=True, hits=100, hits_max=100, x=100, y=100, z=0,
            serial=0x1, gold=0, weight=0, weight_max=400,
            equipment={0x15: 0x500, 1: 0x999},
        )
        world = SimpleNamespace(
            items={},
            nearby_mobiles=lambda x, y, distance=0: [],
        )
        return SimpleNamespace(
            perception=SimpleNamespace(self_state=ss, world=world),
            blackboard={"_was_dead": True},
            bus=None,
        )

    @pytest.mark.asyncio
    async def test_revive_zeroes_stale_seek_resurrection_counter(self, monkeypatch):
        from anima.procedures.base import ProcedureRegistry
        from anima.planner.planner import Planner

        # A corpse is present → Priority 1a returns _RecoverAfterDeath and
        # select_procedure stops there, right after the counter-clear runs.
        corpse = SimpleNamespace(serial=0x900, x=100, y=100)
        monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [corpse])

        planner = Planner(ProcedureRegistry())
        # Simulate 4 failed seek_resurrection attempts surviving across death.
        planner._repeat_counter["seek_resurrection"] = 4

        ctx = self._alive_ctx()
        proc = await planner.select_procedure(ctx)

        assert getattr(proc, "name", None) == "recover_after_death"
        # The stale escalation counter must be zeroed on the dead→alive crossing.
        assert planner._repeat_counter.get("seek_resurrection", 0) == 0
