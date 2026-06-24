"""Regression: roaming must not pick statically unreachable landmarks.

Live symptom: Grimm repeatedly emitted ``I can't reach that`` and the liveness
watchdog cancelled walks to known activity landmarks such as Minoc Mining Camp.
The location was still a valid *semantic* activity target, but the current map
slice had no path to its navigable point from the agent's side of town. The
roaming selector should treat a no-path landmark as temporarily unusable and
try a reachable alternative instead of launching a doomed _MoveToProcedure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import anima.world_knowledge as world_knowledge
from anima.planner.helpers import _MoveToProcedure
from anima.planner.roaming import RoamingHelper
from anima.world_knowledge import Location


def _make_helper() -> RoamingHelper:
    planner = SimpleNamespace(
        _failed_destinations={},
        _move_fail_until=0.0,
    )
    return RoamingHelper(planner)  # type: ignore[arg-type]


def _ctx(x: int, y: int):
    self_state = SimpleNamespace(x=x, y=y, z=0)
    perception = SimpleNamespace(self_state=self_state)
    return SimpleNamespace(
        perception=perception,
        bus=None,
        blackboard={},
        map_reader=object(),
    )


@pytest.mark.asyncio
async def test_activity_roaming_skips_static_no_path_candidate(monkeypatch) -> None:
    unreachable = Location("Bad Mine", x=110, y=100, description="closer but blocked")
    reachable = Location("Good Mine", x=140, y=100, description="farther but reachable")
    monkeypatch.setattr(world_knowledge, "ALL_LOCATIONS", [unreachable, reachable])

    def fake_find_path(_map_reader, sx, sy, tx, ty, **_kwargs):
        assert (sx, sy) == (100, 100)
        if (tx, ty) == (unreachable.nav_x, unreachable.nav_y):
            return None
        if (tx, ty) == (reachable.nav_x, reachable.nav_y):
            return [(101, 100), (reachable.nav_x, reachable.nav_y)]
        raise AssertionError(f"unexpected target {(tx, ty)}")

    monkeypatch.setattr("anima.pathfinding.find_path", fake_find_path)

    result = await _make_helper().try_move_to_activity(_ctx(100, 100))

    assert isinstance(result, _MoveToProcedure)
    assert result.name == "move_to_Good Mine"
    assert (result._x, result._y) == (reachable.nav_x, reachable.nav_y)


@pytest.mark.asyncio
async def test_move_to_location_skips_static_no_path_candidate(monkeypatch) -> None:
    unreachable = Location("Bad Arms", x=110, y=100, description="closer but blocked")
    reachable = Location("Good Arms", x=140, y=100, description="farther but reachable")
    monkeypatch.setattr(world_knowledge, "ALL_LOCATIONS", [unreachable, reachable])

    def fake_find_path(_map_reader, _sx, _sy, tx, ty, **_kwargs):
        if (tx, ty) == (unreachable.nav_x, unreachable.nav_y):
            return None
        if (tx, ty) == (reachable.nav_x, reachable.nav_y):
            return [(101, 100), (reachable.nav_x, reachable.nav_y)]
        raise AssertionError(f"unexpected target {(tx, ty)}")

    monkeypatch.setattr("anima.pathfinding.find_path", fake_find_path)

    result = await _make_helper().move_to_location(_ctx(100, 100), "arms")

    assert isinstance(result, _MoveToProcedure)
    assert result.name == "move_to_Good Arms"
    assert (result._x, result._y) == (reachable.nav_x, reachable.nav_y)
