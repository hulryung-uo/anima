"""Regression: the survival flee/heal gate counts the SAME population combat
fights — so an invulnerable (yellow-health) mobile is NOT a "hostile".

``combat_loop._find_target`` / ``_adjacent_hostiles`` both skip a mobile whose
``is_yellow_health`` is set (a Healer / protected NPC that takes zero damage).
``_is_live_hostile`` (and therefore ``_count_hostiles`` / ``_FleeFromHostiles``)
must mirror that, or a wounded agent flees the immortal survival-arena Healer
co-located at its resurrection spot (kernel K1+/K1/K3) instead of healing in
place on a fight it has already won.
"""
from types import SimpleNamespace

import pytest

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    _FleeFromHostiles,
    _attackable_set,
    _count_hostiles,
    _is_live_hostile,
)


def _mob(serial, x, y, *, notoriety=NotorietyFlag.ENEMY, is_yellow_health=False,
         is_dead=False, body=0):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety,
        is_yellow_health=is_yellow_health, is_dead=is_dead, body=body,
    )


def _ctx(self_x, self_y, mobiles):
    ss = SimpleNamespace(x=self_x, y=self_y, serial=0x1, hits=10, hits_max=100)

    def nearby(x, y, distance=12):
        return [m for m in mobiles if max(abs(m.x - x), abs(m.y - y)) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def test_yellow_health_mobile_is_not_a_hostile():
    ss = SimpleNamespace(x=100, y=100, serial=0x1, hits=10, hits_max=100)
    healer = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ATTACKABLE,
                  is_yellow_health=True)
    assert _is_live_hostile(healer, ss, _attackable_set()) is False


def test_normal_enemy_is_still_a_hostile():
    ss = SimpleNamespace(x=100, y=100, serial=0x1, hits=10, hits_max=100)
    ettin = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ENEMY)
    assert _is_live_hostile(ettin, ss, _attackable_set()) is True


def test_count_hostiles_ignores_adjacent_immortal_healer():
    """One real swarm mob + an adjacent immortal Healer: only the mob counts,
    so a wounded agent is NOT held above the flee/swarm floor by the Healer."""
    swarm = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ENEMY)
    healer = _mob(0x3, 100, 101, notoriety=NotorietyFlag.ATTACKABLE,
                  is_yellow_health=True)
    ctx = _ctx(100, 100, [swarm, healer])
    assert _count_hostiles(ctx, ctx.perception.self_state, dist=6) == 1


@pytest.mark.asyncio
async def test_flee_run_finds_no_hostiles_when_only_immortal_healer_present():
    """After clearing the swarm, only the immortal Healer remains adjacent.
    The flee procedure must report "nothing to flee from" rather than dragging
    the wounded agent away from the Healer it should be healing beside."""
    healer = _mob(0x3, 101, 100, notoriety=NotorietyFlag.ATTACKABLE,
                  is_yellow_health=True)
    ctx = _ctx(100, 100, [healer])
    result = await _FleeFromHostiles().run(ctx)
    assert result.success is True
    assert "No hostiles" in result.message
