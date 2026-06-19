"""Regression: the flee escape vector is computed over the SAME close-swarm
radius that triggered the flee.

``Planner._should_flee_swarm`` keys the decision on hostiles within
``_FLEE_SCAN_DIST`` tiles. ``_FleeFromHostiles.run`` used to recompute the
escape centroid over a *wider* 12-tile scan, so a distant hostile on the far
side of the agent could drag the centroid past the agent and flip the
"away" direction to point straight back into the close swarm that wounded it.
These tests pin the run-side scan to the same radius the gate uses.
"""
from types import SimpleNamespace

import pytest

import anima.action.movement as movement
from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    _FLEE_SCAN_DIST,
    _FleeFromHostiles,
    _flee_destination,
)


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, is_dead=False):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety, is_dead=is_dead)


def _ctx(self_x, self_y, mobiles):
    ss = SimpleNamespace(x=self_x, y=self_y, serial=0x1, hits=10, hits_max=100)

    def nearby(x, y, distance=12):
        return [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


class TestFleeScanRadius:
    @pytest.mark.asyncio
    async def test_far_mob_does_not_invert_escape_into_close_swarm(self, monkeypatch):
        """3 mobs hugging the agent to the EAST (the swarm that triggers the
        flee) + 1 mob far to the WEST (within 12 tiles, outside the 6-tile
        gate). The agent must flee WEST (away from the close swarm), never EAST
        toward it. The old 12-tile centroid scan let the far west mob drag the
        centroid west of the agent, flipping the escape vector east into the
        swarm.
        """
        calls = {}

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            calls["dest"] = (x, y)
            return True

        monkeypatch.setattr(movement, "go_to", fake_go_to)

        close_swarm = [
            _mob(0x2, 102, 100), _mob(0x3, 103, 100), _mob(0x4, 102, 101),
        ]
        far_mob = _mob(0x5, 89, 100)  # 11 tiles WEST: inside 12, outside 6
        ctx = _ctx(100, 100, close_swarm + [far_mob])

        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        fx, _fy = calls["dest"]
        # Flee WEST, away from the close eastern swarm — never east toward it.
        assert fx < 100, f"fled toward the close swarm: dest={calls['dest']}"

    @pytest.mark.asyncio
    async def test_run_ignores_hostiles_beyond_scan_radius(self, monkeypatch):
        """A lone hostile just outside ``_FLEE_SCAN_DIST`` is not part of the
        swarm and must not be the thing the agent computes its flee from."""
        captured = {}

        def fake_flee_destination(ss, hostiles, flee_dist):
            captured["serials"] = sorted(m.serial for m in hostiles)
            return ss.x - 1, ss.y

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return True

        import anima.planner.planner as planner_mod
        monkeypatch.setattr(planner_mod, "_flee_destination", fake_flee_destination)
        monkeypatch.setattr(movement, "go_to", fake_go_to)

        inside = _mob(0x2, 100 + _FLEE_SCAN_DIST, 100)       # on the edge: counts
        outside = _mob(0x3, 100 + _FLEE_SCAN_DIST + 1, 100)  # one past: excluded
        ctx = _ctx(100, 100, [inside, outside])

        await _FleeFromHostiles().run(ctx)
        assert captured["serials"] == [0x2]

    def test_scan_radius_matches_gate_constant(self):
        # The run-side scan and the gate both key on this one radius. Guard the
        # contract so a future edit can't silently re-widen one side.
        assert _FLEE_SCAN_DIST == 6

    def test_flee_destination_unchanged_for_pure_close_swarm(self):
        # Sanity: with only the close swarm, the direction is correct (east
        # swarm -> flee west). This is the behaviour the radius fix preserves.
        ss = SimpleNamespace(x=100, y=100, serial=0x1, hits=10, hits_max=100)
        swarm = [_mob(0x2, 102, 100), _mob(0x3, 103, 100), _mob(0x4, 102, 101)]
        fx, _fy = _flee_destination(ss, swarm, flee_dist=12)
        assert fx < ss.x
