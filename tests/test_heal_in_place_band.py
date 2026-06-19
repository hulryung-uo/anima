"""Priority-1 survival threshold interaction: the heal-in-place floor must
agree with combat's re-engage floor, closing the 30-40% HP "dead zone".

Combat (anima/procedures/combat_loop.py) breaks off at RETREAT_HP_PCT (35%) and
hunt_nearby.can_start refuses to (re)start a fight below 40% HP. The planner's
Priority-1b flee gate only fires when SWARMED (>= _FLEE_HOSTILE_COUNT hostiles).
The heal-in-place gate used to be a hardcoded < 0.30, so a NON-swarmed agent at
30-40% HP (one or two mobs, or a kiting mob out of melee) could neither re-hunt
(too wounded), nor flee (not swarmed), nor heal — it fell through to the
profession/mining loop and wandered wounded until it re-aggroed and died.
_should_heal_in_place now uses _HEAL_IN_PLACE_HP_PCT == _FLEE_HP_PCT (0.40), so
the whole 30-40% band heals in place instead.
"""
from types import SimpleNamespace

from anima.planner.planner import (
    Planner,
    _FLEE_HP_PCT,
    _HEAL_IN_PLACE_HP_PCT,
)
from anima.procedures.base import ProcedureRegistry


def _ss(hits, hits_max=100):
    return SimpleNamespace(is_alive=True, x=100, y=100, serial=0x1,
                           hits=hits, hits_max=hits_max)


def _planner():
    return Planner(ProcedureRegistry())


def test_heal_floor_matches_combat_reengage_floor():
    # The heal-in-place floor and the flee floor are one shared number so the
    # two survival branches can't disagree about "too wounded to fight".
    assert _HEAL_IN_PLACE_HP_PCT == _FLEE_HP_PCT
    assert _HEAL_IN_PLACE_HP_PCT == 0.40


def test_heals_in_the_30_to_40_dead_zone():
    # This is the regression: 30-40% HP, NOT swarmed. Combat won't re-hunt
    # (needs >= 40%); the flee gate won't fire (not swarmed). Old gate (< 0.30)
    # left the agent to fall through wounded. Now it heals in place.
    planner = _planner()
    for hits in (30, 34, 35, 39):  # entire dead band, incl. the 35% retreat floor
        assert planner._should_heal_in_place(_ss(hits)) is True, hits


def test_still_heals_below_30():
    # The old behaviour (< 30% always heals) is preserved.
    planner = _planner()
    assert planner._should_heal_in_place(_ss(10)) is True
    assert planner._should_heal_in_place(_ss(29)) is True


def test_does_not_heal_at_or_above_the_floor():
    # At/above 40% the agent is healthy enough to fight — don't pre-empt the
    # profession/combat loop with a heal.
    planner = _planner()
    assert planner._should_heal_in_place(_ss(40)) is False
    assert planner._should_heal_in_place(_ss(80)) is False
    assert planner._should_heal_in_place(_ss(100)) is False


def test_unknown_hp_does_not_trigger_heal():
    # hits_max <= 0 means we have no status read yet — never heal on a guess
    # (matches the flee gate's hits_max > 0 guard).
    planner = _planner()
    assert planner._should_heal_in_place(_ss(0, hits_max=0)) is False
