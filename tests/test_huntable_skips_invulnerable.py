"""Regression: the planner's deadlock-recovery hunt scan must skip an
invulnerable (yellow-health) mobile, mirroring combat_loop._find_target /
_adjacent_hostiles and the survival flee gate (_is_live_hostile).

ServUO marks Healers / protected town NPCs with the YELLOW_BAR flag
(MobileInfo.is_yellow_health) — they take zero damage however long we swing,
yet can read as gray/ATTACKABLE notoriety. The survival-arena Healer NPC is
co-located at the agent's resurrection spot (kernel K1+/K1/K3); without this
guard _find_huntable_target picks the immortal Healer as a target and the
deadlock-recovery hunt branch burns ticks swinging at it forever — never a
kill, never gold, never escalation.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _mob(serial, x, y, *, notoriety=NotorietyFlag.ENEMY,
         is_yellow_health=False, body=0):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, body=body,
        is_yellow_health=is_yellow_health,
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


def test_immortal_healer_is_not_huntable():
    """The lone candidate is an immortal Healer → no huntable target."""
    healer = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ATTACKABLE,
                  is_yellow_health=True)
    ctx = _ctx(100, 100, [healer])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is None


def test_real_enemy_beside_immortal_healer_is_picked():
    """A real enemy and an adjacent immortal Healer: the enemy is chosen, the
    Healer is never selected even though it sorts closer."""
    healer = _mob(0x2, 100, 100, notoriety=NotorietyFlag.ATTACKABLE,
                  is_yellow_health=True)  # on top of the agent (dist 0)
    ettin = _mob(0x3, 103, 100, notoriety=NotorietyFlag.ENEMY)
    ctx = _ctx(100, 100, [healer, ettin])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is not None
    assert target.serial == ettin.serial


def test_normal_enemy_is_still_huntable():
    """Guard does not over-exclude: a normal (non-yellow) enemy is huntable."""
    ettin = _mob(0x3, 102, 100, notoriety=NotorietyFlag.ENEMY)
    ctx = _ctx(100, 100, [ettin])
    planner = _planner()
    target = planner._find_huntable_target(
        ctx, ctx.perception.self_state, include_small_animals=True,
    )
    assert target is not None
    assert target.serial == ettin.serial
