"""Priority-1b survival inversion: a fully-surrounded agent that cannot path
away from the swarm used to re-select _FleeFromHostiles every tick forever
(the proc reports success on the no-op move, so the starvation breaker never
demotes it). It "fled in place" until the swarm killed it, never reaching the
heal-in-place block directly below the gate. _should_flee_swarm caps the
consecutive flee selections so the agent eventually falls through to heal.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import Planner, _FLEE_MAX_CONSECUTIVE
from anima.procedures.base import ProcedureRegistry


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety)


def _ctx(hits, mobiles):
    ss = SimpleNamespace(
        is_alive=True, x=100, y=100, serial=0x1, hits=hits, hits_max=100,
    )
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=0: [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ],
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def _planner():
    return Planner(ProcedureRegistry())


SWARM = [_mob(0x2, 100, 100), _mob(0x3, 101, 100), _mob(0x4, 100, 101)]


def test_flees_while_under_the_cap():
    planner = _planner()
    ctx = _ctx(hits=30, mobiles=SWARM)  # 30% HP < 40% floor, 3 adjacent hostiles
    # The first _FLEE_MAX_CONSECUTIVE selections all choose to flee.
    for i in range(_FLEE_MAX_CONSECUTIVE):
        assert planner._should_flee_swarm(ctx, ctx.perception.self_state) is True, i
    assert planner._flee_consecutive == _FLEE_MAX_CONSECUTIVE


def test_gives_up_after_the_cap_so_it_can_heal_in_place():
    planner = _planner()
    ctx = _ctx(hits=30, mobiles=SWARM)
    # Burn through the cap (flee kept failing to break contact).
    for _ in range(_FLEE_MAX_CONSECUTIVE):
        planner._should_flee_swarm(ctx, ctx.perception.self_state)
    # The next tick must NOT flee — fall through to heal-in-place instead of
    # fleeing in place forever.
    assert planner._should_flee_swarm(ctx, ctx.perception.self_state) is False


def test_counter_resets_when_contact_broken():
    planner = _planner()
    swarmed = _ctx(hits=30, mobiles=SWARM)
    for _ in range(_FLEE_MAX_CONSECUTIVE):
        planner._should_flee_swarm(swarmed, swarmed.perception.self_state)
    # Contact broken (swarm gone): counter resets, so a later swarm gets a
    # fresh budget of flee attempts instead of being starved by stale state.
    clear = _ctx(hits=30, mobiles=[])
    assert planner._should_flee_swarm(clear, clear.perception.self_state) is False
    assert planner._flee_consecutive == 0
    again = _ctx(hits=30, mobiles=SWARM)
    assert planner._should_flee_swarm(again, again.perception.self_state) is True


def test_counter_resets_when_healed_above_floor():
    planner = _planner()
    swarmed = _ctx(hits=30, mobiles=SWARM)
    for _ in range(_FLEE_MAX_CONSECUTIVE):
        planner._should_flee_swarm(swarmed, swarmed.perception.self_state)
    # HP back above the 40% flee floor → no longer "wounded": reset + don't flee.
    healed = _ctx(hits=80, mobiles=SWARM)
    assert planner._should_flee_swarm(healed, healed.perception.self_state) is False
    assert planner._flee_consecutive == 0


def test_does_not_flee_below_hostile_threshold():
    planner = _planner()
    # Wounded but only 2 hostiles (< _FLEE_HOSTILE_COUNT) → heal in place.
    ctx = _ctx(hits=30, mobiles=SWARM[:2])
    assert planner._should_flee_swarm(ctx, ctx.perception.self_state) is False
    assert planner._flee_consecutive == 0
