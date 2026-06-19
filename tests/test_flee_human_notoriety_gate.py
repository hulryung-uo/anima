"""Friend/foe symmetry for the survival flee gate.

The combat loop never attacks a bare-gray (ATTACKABLE) *human* body — it
engages humans only when they are CRIMINAL/ENEMY/MURDERER. The survival flee
gate (``_is_live_hostile`` -> ``_count_hostiles`` -> ``_should_flee_swarm``)
must scope its "swarm" to the same population: a wounded agent must not be
driven to flee, with heal-in-place suppressed, by a crowd of harmless gray
townsfolk/passers-by it would never raise a weapon against.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    Planner,
    _count_hostiles,
    _is_live_hostile,
    _attackable_set,
)
from anima.procedures.base import ProcedureRegistry

HUMAN_BODY = 0x0190
MONSTER_BODY = 0x0009  # an ettin, e.g. — a non-human body id


def _mob(serial, x, y, notoriety, body=MONSTER_BODY, is_dead=False):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, body=body, is_dead=is_dead
    )


def _ss():
    return SimpleNamespace(
        is_alive=True, x=100, y=100, serial=0x1, hits=30, hits_max=100
    )


def _ctx(mobiles):
    ss = _ss()
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=0: [
            m for m in mobiles if max(abs(m.x - x), abs(m.y - y)) <= distance
        ],
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def test_bare_gray_human_is_not_a_flee_hostile():
    """A gray (ATTACKABLE) human is not a foe combat fights, so not one we flee."""
    ss = _ss()
    gray_human = _mob(0x2, 100, 100, NotorietyFlag.ATTACKABLE, body=HUMAN_BODY)
    assert _is_live_hostile(gray_human, ss, _attackable_set()) is False


def test_hostile_human_still_counts():
    """A CRIMINAL/ENEMY/MURDERER human IS a foe combat fights -> still flee-able."""
    ss = _ss()
    for noto in (NotorietyFlag.CRIMINAL, NotorietyFlag.ENEMY, NotorietyFlag.MURDERER):
        red_human = _mob(0x2, 100, 100, noto, body=HUMAN_BODY)
        assert _is_live_hostile(red_human, ss, _attackable_set()) is True, noto


def test_gray_monster_still_counts():
    """The human carve-out must NOT leak to monsters: a gray ettin is a real foe."""
    ss = _ss()
    gray_monster = _mob(0x2, 100, 100, NotorietyFlag.ATTACKABLE, body=MONSTER_BODY)
    assert _is_live_hostile(gray_monster, ss, _attackable_set()) is True


def test_crowd_of_gray_humans_does_not_trigger_flee():
    """Three adjacent gray humans (>= _FLEE_HOSTILE_COUNT) must NOT be a swarm:
    the wounded agent should heal in place, not flee from non-combatants."""
    grays = [
        _mob(0x2, 100, 100, NotorietyFlag.ATTACKABLE, body=HUMAN_BODY),
        _mob(0x3, 101, 100, NotorietyFlag.ATTACKABLE, body=HUMAN_BODY),
        _mob(0x4, 100, 101, NotorietyFlag.ATTACKABLE, body=HUMAN_BODY),
    ]
    ctx = _ctx(grays)
    ss = ctx.perception.self_state
    assert _count_hostiles(ctx, ss, dist=6) == 0
    planner = Planner(ProcedureRegistry())
    assert planner._should_flee_swarm(ctx, ss) is False


def test_swarm_of_monsters_still_triggers_flee():
    """Regression guard: a real monster swarm must still trip the flee gate."""
    monsters = [
        _mob(0x2, 100, 100, NotorietyFlag.ENEMY),
        _mob(0x3, 101, 100, NotorietyFlag.ENEMY),
        _mob(0x4, 100, 101, NotorietyFlag.ENEMY),
    ]
    ctx = _ctx(monsters)
    ss = ctx.perception.self_state
    assert _count_hostiles(ctx, ss, dist=6) == 3
    planner = Planner(ProcedureRegistry())
    assert planner._should_flee_swarm(ctx, ss) is True
