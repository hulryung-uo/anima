"""Regression: a poisoned, swarmed agent flees before trying to cure in place.

The Priority-1c poison-cure gate applies ``bandage_self`` the instant the agent
is poisoned, even at full HP. But a bandage (heal OR cure) is cancelled by
adjacent melee — the very reason the Priority-1b flee branch exists. A poisoned
agent at >= _FLEE_HP_PCT HP surrounded by a swarm used to slip past the HP-only
flee floor and the heal-in-place floor, reach the cure gate, and re-attempt a
doomed in-place cure-bandage every tick while the swarm beat it down. The flee
gate now also fires on poison-while-swarmed so the agent breaks contact first.
"""
from types import SimpleNamespace

import pytest

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    Planner,
    _FLEE_HOSTILE_COUNT,
    _FLEE_HP_PCT,
    _FLEE_SCAN_DIST,
)


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, is_dead=False):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety, is_dead=is_dead)


def _ss(hits, hits_max=100, is_poisoned=False):
    return SimpleNamespace(
        x=100, y=100, serial=0x1, hits=hits, hits_max=hits_max, is_poisoned=is_poisoned
    )


def _ctx(ss, mobiles):
    def nearby(x, y, distance=12):
        return [m for m in mobiles if max(abs(m.x - x), abs(m.y - y)) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def _swarm(n):
    # n live hostiles all hugging the agent (within the flee scan radius).
    return [_mob(0x10 + i, 101 + (i % 3), 100 + (i // 3)) for i in range(n)]


class TestFleeSwarmPoison:
    def test_poisoned_and_swarmed_at_high_hp_flees(self):
        # High HP (above the flee/heal floor) but poisoned + swarmed: must flee
        # so the cure-bandage isn't endlessly cancelled by adjacent melee.
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 0
        ss = _ss(hits=95, is_poisoned=True)  # 95% HP, well above _FLEE_HP_PCT
        ctx = _ctx(ss, _swarm(_FLEE_HOSTILE_COUNT))
        assert ss.hits >= ss.hits_max * _FLEE_HP_PCT  # the gap this closes
        assert planner._should_flee_swarm(ctx, ss) is True

    def test_poisoned_but_not_swarmed_does_not_flee(self):
        # Poisoned with too few hostiles: the un-swarmed cure gate handles it;
        # the flee branch must NOT preempt (a bandage CAN land out of melee).
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 0
        ss = _ss(hits=95, is_poisoned=True)
        ctx = _ctx(ss, _swarm(_FLEE_HOSTILE_COUNT - 1))
        assert planner._should_flee_swarm(ctx, ss) is False

    def test_healthy_unpoisoned_swarmed_does_not_flee(self):
        # The original contract: a healthy, un-poisoned agent does not flee a
        # swarm — it keeps fighting. Poison folding must not change this.
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 0
        ss = _ss(hits=95, is_poisoned=False)
        ctx = _ctx(ss, _swarm(_FLEE_HOSTILE_COUNT))
        assert planner._should_flee_swarm(ctx, ss) is False

    def test_wounded_swarmed_still_flees(self):
        # The pre-existing HP-floor trigger is preserved.
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 0
        ss = _ss(hits=10, is_poisoned=False)  # 10% HP, below _FLEE_HP_PCT
        ctx = _ctx(ss, _swarm(_FLEE_HOSTILE_COUNT))
        assert planner._should_flee_swarm(ctx, ss) is True

    def test_consecutive_cap_resets_when_swarm_clears(self):
        # Poison no longer present and no swarm -> counter resets, no flee.
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 3
        ss = _ss(hits=95, is_poisoned=False)
        ctx = _ctx(ss, [])
        assert planner._should_flee_swarm(ctx, ss) is False
        assert planner._flee_consecutive == 0

    def test_unknown_hits_max_does_not_flee(self):
        planner = Planner.__new__(Planner)
        planner._flee_consecutive = 0
        ss = _ss(hits=0, hits_max=0, is_poisoned=True)
        ctx = _ctx(ss, _swarm(_FLEE_HOSTILE_COUNT))
        assert planner._should_flee_swarm(ctx, ss) is False

    def test_scan_radius_unchanged(self):
        assert _FLEE_SCAN_DIST == 6
