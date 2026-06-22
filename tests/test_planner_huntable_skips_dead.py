"""Regression: the planner's deadlock-recovery hunt scan must skip an
already-dead mobile, mirroring combat_loop._find_target / _adjacent_hostiles.

A just-felled monster lingers in world.mobiles (KNOWN health bar at zero, or a
ghost body) until its 0x1D Delete arrives. combat_loop._find_target skips it
(`getattr(m, "is_dead", False) is True`); the planner's twin scan,
_find_huntable_target, did not. Because the hunt scan ranks the closest
candidate first, a corpse the agent is standing on sorts to the very top, so
the planner dispatched _HuntForGold at a dead body while the combat loop's own
_find_target returned None — burning the movement budget re-selecting the same
corpse every tick until the Delete landed. This locks the cross-module 'is this
mobile a valid target' predicate between the two scans.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _mob(serial, x, y, *, notoriety=NotorietyFlag.ENEMY,
         is_dead=False, body=0):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, body=body,
        is_yellow_health=False, is_dead=is_dead,
    )


def _ctx(self_x, self_y, mobiles):
    ss = SimpleNamespace(x=self_x, y=self_y, z=0, serial=0x1)

    def nearby(x, y, distance=18):
        return [m for m in mobiles if max(abs(m.x - x), abs(m.y - y)) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        # No map_reader → _find_huntable_target returns candidates[0] directly
        # (skips the A* reachability filter), keeping the test pure.
        map_reader=None,
    )


def _planner():
    return Planner(ProcedureRegistry())


def test_dead_mob_is_not_huntable():
    """The lone candidate is a corpse (dead) → no huntable target."""
    corpse = _mob(0x2, 101, 100, is_dead=True)
    ctx = _ctx(100, 100, [corpse])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is None


def test_live_enemy_chosen_over_closer_corpse():
    """A corpse the agent stands on (dist 0) and a live enemy farther away:
    the live enemy is chosen, never the corpse that sorts closer."""
    corpse = _mob(0x2, 100, 100, is_dead=True)   # on top of the agent (dist 0)
    ettin = _mob(0x3, 103, 100, is_dead=False)
    ctx = _ctx(100, 100, [corpse, ettin])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is not None
    assert target.serial == ettin.serial


def test_live_enemy_is_still_huntable():
    """Guard does not over-exclude: a normal (live) enemy is huntable."""
    ettin = _mob(0x3, 102, 100, is_dead=False)
    ctx = _ctx(100, 100, [ettin])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is not None
    assert target.serial == ettin.serial


def test_mob_without_is_dead_field_not_excluded():
    """A test/transient mob lacking the is_dead field is NOT mis-excluded
    (`is True` discipline matches combat_loop._find_target)."""
    bare = SimpleNamespace(serial=0x4, x=102, y=100, body=0,
                           notoriety=NotorietyFlag.ENEMY,
                           is_yellow_health=False)  # no is_dead attr
    ctx = _ctx(100, 100, [bare])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is not None
    assert target.serial == 0x4
