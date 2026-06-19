"""Tests for anti-surround positioning in the melee combat loop.

A melee mob can only swing the agent while it is adjacent (Chebyshev distance
1), so each extra adjacent hostile is another full damage stream. When the pack
boxes the agent in (>= SURROUND_COUNT adjacent), ``_reposition_if_surrounded``
steps one tile away from the hostile centroid so fewer of them are in melee
range — without fleeing the engagement.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import (
    SURROUND_COUNT,
    _adjacent_hostiles,
    _reposition_dest,
    _reposition_if_surrounded,
)


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, body=0x0021, is_dead=False):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, body=body, is_dead=is_dead,
    )


def _ctx(self_x=100, self_y=100, mobiles=()):
    ss = SimpleNamespace(x=self_x, y=self_y, serial=0x1)

    def nearby(x, y, distance=18):
        return [
            m for m in mobiles
            if abs(m.x - x) <= distance and abs(m.y - y) <= distance
        ]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


class TestAdjacentHostiles:
    def test_counts_only_melee_range_attackables(self):
        ctx = _ctx(mobiles=[
            _mob(0x2, 100, 101),                 # adjacent
            _mob(0x3, 101, 101),                 # adjacent (diagonal)
            _mob(0x4, 103, 100),                 # 3 tiles away — not adjacent
        ])
        assert {m.serial for m in _adjacent_hostiles(ctx)} == {0x2, 0x3}

    def test_skips_self_dead_and_non_hostile(self):
        ctx = _ctx(mobiles=[
            _mob(0x1, 100, 100),                                  # self serial
            _mob(0x2, 100, 101, is_dead=True),                    # corpse
            _mob(0x3, 101, 100, notoriety=NotorietyFlag.INNOCENT),  # not hostile
            _mob(0x4, 99, 100),                                   # live hostile
        ])
        assert {m.serial for m in _adjacent_hostiles(ctx)} == {0x4}


class TestRepositionDest:
    def test_steps_away_from_centroid(self):
        ss = SimpleNamespace(x=100, y=100, serial=0x1)
        # Pack clustered to the SE → agent should back off to the NW.
        hostiles = [_mob(0x2, 101, 101), _mob(0x3, 101, 100), _mob(0x4, 100, 101)]
        dest = _reposition_dest(ss, hostiles)
        assert dest is not None
        dx = dest[0] - ss.x
        dy = dest[1] - ss.y
        assert dx < 0 and dy < 0                 # moved NW, away from the pack
        assert max(abs(dx), abs(dy)) == cl.REPOSITION_STEP

    def test_no_away_direction_when_balanced(self):
        # Hostiles symmetric around the agent → centroid == agent tile → no move.
        ss = SimpleNamespace(x=100, y=100, serial=0x1)
        hostiles = [_mob(0x2, 99, 100), _mob(0x3, 101, 100)]
        assert _reposition_dest(ss, hostiles) is None


class TestRepositionIfSurrounded:
    @pytest.mark.asyncio
    async def test_steps_out_when_surrounded(self, monkeypatch):
        calls = {}

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            calls["dest"] = (x, y)
            return True

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        # 3 hostiles clustered SE of the agent → surrounded.
        ctx = _ctx(mobiles=[
            _mob(0x2, 101, 100), _mob(0x3, 100, 101), _mob(0x4, 101, 101),
        ])
        assert len(_adjacent_hostiles(ctx)) >= SURROUND_COUNT
        stepped = await _reposition_if_surrounded(ctx)
        assert stepped is True
        # Backed off to the NW (away from the SE pack).
        assert calls["dest"][0] < 100 and calls["dest"][1] < 100

    @pytest.mark.asyncio
    async def test_holds_when_not_surrounded(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            raise AssertionError("should not move below the surround threshold")

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        # Only 2 adjacent (below SURROUND_COUNT=3) → keep swinging in place.
        ctx = _ctx(mobiles=[_mob(0x2, 101, 100), _mob(0x3, 100, 101)])
        assert await _reposition_if_surrounded(ctx) is False
