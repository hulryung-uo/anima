"""Persona-field plumbing: combat_disposition must gate the planner's PROACTIVE
combat procedures, not just the legacy BT melee skill.

A persona declared ``combat_disposition: pacifist`` "never initiates combat"
(documented on anima.persona.Persona and honored in skills/combat/melee.py).
The active path is the v2 planner, which runs HuntNearby (engage a hostile in
range) and WanderForCombat (roam to FIND a fight). Before the fix both
procedures ignored the field entirely, so a pacifist blacksmith/bard/thief
still proactively started fights. These tests pin the gate at can_start so it
holds no matter which planner path invokes the procedure.

Conservative scope: only ``pacifist`` is gated. ``defensive`` /
``aggressive`` / unset / no-persona stay PERMISSIVE so the COMBAT eval lineage
behaves exactly as before.
"""
from types import SimpleNamespace

import pytest

from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby, WanderForCombat


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, body=0x0021):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety, body=body)


def _ctx(*, disposition=None, mobiles=(), self_x=100, self_y=100):
    ss = SimpleNamespace(
        is_alive=True, hits_max=100, hp_percent=100.0,
        x=self_x, y=self_y, serial=0x1,
        equipment={1: 0x999},  # armed
    )
    world = SimpleNamespace(nearby_mobiles=lambda x, y, distance=0: list(mobiles))
    persona = None if disposition is None else SimpleNamespace(combat_disposition=disposition)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        persona=persona,
    )


class TestPacifistNeverInitiates:
    @pytest.mark.asyncio
    async def test_hunt_off_for_pacifist_even_with_target_in_range(self):
        ctx = _ctx(disposition="pacifist", mobiles=[_mob(0x2, 102, 101)])
        assert await HuntNearby().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_wander_off_for_pacifist_with_no_target(self):
        ctx = _ctx(disposition="pacifist", mobiles=[])
        assert await WanderForCombat().can_start(ctx) is False


class TestNonPacifistStaysPermissive:
    @pytest.mark.asyncio
    async def test_hunt_on_for_aggressive_with_target(self):
        ctx = _ctx(disposition="aggressive", mobiles=[_mob(0x2, 102, 101)])
        assert await HuntNearby().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_hunt_on_for_defensive_with_target(self):
        # defensive is NOT gated at proactive-start (retaliation lives in the
        # survival chain); only pacifist is a hard "never start".
        ctx = _ctx(disposition="defensive", mobiles=[_mob(0x2, 102, 101)])
        assert await HuntNearby().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_wander_on_for_defensive_no_target(self):
        ctx = _ctx(disposition="defensive", mobiles=[])
        assert await WanderForCombat().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_unset_persona_field_defaults_permissive(self):
        ctx = _ctx(disposition=None, mobiles=[_mob(0x2, 102, 101)])
        assert await HuntNearby().can_start(ctx) is True
