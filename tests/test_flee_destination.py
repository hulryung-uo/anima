"""Tests for the survival-floor flee gate (``_flee_destination`` / ``_FleeFromHostiles``).

The HP-floor guard only fires when the agent is wounded *and* swarmed — the
exact geometry where it tends to sit on the hostile centroid. The destination
computation must still break contact in that degenerate case instead of
"fleeing" to its own tile (a no-op that gets the agent killed mid-bandage).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
from anima.perception.enums import NotorietyFlag
from anima.planner.planner import _FleeFromHostiles, _flee_destination


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety)


def _ss(x=100, y=100, serial=0x1):
    return SimpleNamespace(x=x, y=y, serial=serial, hits=10, hits_max=100)


def _chebyshev(ax, ay, bx, by) -> int:
    return max(abs(ax - bx), abs(ay - by))


class TestFleeDestination:
    def test_moves_away_from_offset_pack(self):
        ss = _ss()
        # Pack clustered to the SE → flee NW.
        hostiles = [_mob(0x2, 103, 103), _mob(0x3, 104, 102), _mob(0x4, 102, 104)]
        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        assert fx < ss.x and fy < ss.y

    def test_surrounded_centroid_does_not_flee_in_place(self):
        """Agent sits exactly on the centroid of a symmetric swarm.

        Old behaviour: away-vector is (0,0) → destination == agent tile → the
        agent never moves and dies. The destination must be a real tile some
        distance away.
        """
        ss = _ss(x=100, y=100)
        hostiles = [
            _mob(0x2, 99, 100), _mob(0x3, 101, 100),
            _mob(0x4, 100, 99), _mob(0x5, 100, 101),
        ]
        # Sanity: this swarm's centroid really is the agent's tile.
        cx = sum(m.x for m in hostiles) / len(hostiles)
        cy = sum(m.y for m in hostiles) / len(hostiles)
        assert (cx, cy) == (ss.x, ss.y)

        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        # Must actually break contact, not flee onto its own tile.
        assert (fx, fy) != (ss.x, ss.y)
        assert _chebyshev(ss.x, ss.y, fx, fy) >= 1

    def test_flee_distance_respected_on_offset_pack(self):
        ss = _ss(x=100, y=100)
        hostiles = [_mob(0x2, 110, 100)]  # due east → flee due west
        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        assert fy == 100
        assert ss.x - fx == 12


def _ctx(self_x, self_y, mobiles):
    ss = _ss(x=self_x, y=self_y)

    def nearby(x, y, distance=12):
        return [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
    )


class TestFleeFromHostilesRun:
    @pytest.mark.asyncio
    async def test_run_when_surrounded_moves_off_tile(self, monkeypatch):
        calls = {}

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            calls["dest"] = (x, y)
            return True

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(100, 100, [
            _mob(0x2, 99, 100), _mob(0x3, 101, 100),
            _mob(0x4, 100, 99), _mob(0x5, 100, 101),
        ])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        # The agent must NOT have "fled" to its own tile.
        assert calls["dest"] != (100, 100)
        assert _chebyshev(100, 100, *calls["dest"]) >= 1
        assert result.next_suggestion == "bandage_self"

    @pytest.mark.asyncio
    async def test_run_no_hostiles_is_noop(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            raise AssertionError("should not move when nothing to flee from")

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(100, 100, [])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        assert "No hostiles" in result.message
