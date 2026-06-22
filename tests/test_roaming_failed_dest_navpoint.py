"""Regression: failed-destination dedup must key on the NAVIGABLE point.

``RoamingHelper.move_to_location`` returns a ``_MoveToProcedure`` aimed at the
location's ``nav_x``/``nav_y`` (the outdoor approach point for indoor
locations). When that walk fails, the planner records the failure under those
SAME nav coords (``_record_failed_destination(proc._x, proc._y)``). The dedup
filter that skips a recently-failed destination must therefore look up the nav
coords too — keying it on the raw ``x``/``y`` meant an approach-point location
whose move just failed was never recognised as failed and got re-selected every
tick, replaying the doomed-path ping-pong the dedup exists to stop.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import anima.world_knowledge as world_knowledge
from anima.planner.helpers import _MoveToProcedure
from anima.planner.roaming import RoamingHelper
from anima.world_knowledge import Location


def _make_helper() -> RoamingHelper:
    # A bare planner stand-in carrying just the state RoamingHelper touches.
    planner = SimpleNamespace(
        _failed_destinations={},
        _move_fail_until=0.0,
    )
    return RoamingHelper(planner)  # type: ignore[arg-type]


def _ctx(x: int, y: int):
    self_state = SimpleNamespace(x=x, y=y)
    perception = SimpleNamespace(self_state=self_state)
    return SimpleNamespace(perception=perception, bus=None, blackboard={})


@pytest.mark.asyncio
async def test_failed_indoor_destination_is_skipped(monkeypatch):
    """An indoor vendor whose move-to (nav point) failed must NOT be reselected.

    The recorded failure key is the nav point (what the move walked to). With
    the dedup keyed on nav coords, that single matching vendor is filtered out
    and there are no candidates left -> move_to_location returns None instead of
    re-issuing the same doomed walk.
    """
    # Indoor location: interior coords (500,500) differ from the outdoor
    # approach point (400,400). The move target / failure key is the approach.
    vendor = Location(
        "Test Vendor",
        x=500,
        y=500,
        description="indoor",
        approach_x=400,
        approach_y=400,
    )
    assert (vendor.nav_x, vendor.nav_y) != (vendor.x, vendor.y)

    # move_to_location imports ALL_LOCATIONS lazily from world_knowledge.
    monkeypatch.setattr(world_knowledge, "ALL_LOCATIONS", [vendor])

    helper = _make_helper()
    # Within max_dist (300) of the approach point but not "already here"
    # (dist > 3): nav point (400,400), agent at (350,350) -> Chebyshev 50.
    ctx = _ctx(x=350, y=350)

    # Simulate the planner having recorded a failed move to the NAV point — the
    # exact key _record_failed_destination(proc._x, proc._y) writes.
    helper._planner._failed_destinations[(vendor.nav_x, vendor.nav_y)] = time.time()

    result = await helper.move_to_location(ctx, "vendor")

    # Keyed on nav coords, the only candidate is filtered out -> None.
    # (Before the fix the dedup checked (x, y), missed the (nav_x, nav_y) key,
    #  and re-issued the same doomed move.)
    assert result is None


@pytest.mark.asyncio
async def test_unfailed_indoor_destination_targets_navpoint(monkeypatch):
    """Sanity: with no recorded failure the move aims at the approach point."""
    vendor = Location(
        "Test Vendor",
        x=500,
        y=500,
        description="indoor",
        approach_x=400,
        approach_y=400,
    )
    # move_to_location imports ALL_LOCATIONS lazily from world_knowledge.
    monkeypatch.setattr(world_knowledge, "ALL_LOCATIONS", [vendor])

    helper = _make_helper()
    ctx = _ctx(x=350, y=350)

    result = await helper.move_to_location(ctx, "vendor")

    assert isinstance(result, _MoveToProcedure)
    # The move walks to the nav point, which is also the failure-record key —
    # so the dedup key and the move target are one and the same.
    assert (result._x, result._y) == (vendor.nav_x, vendor.nav_y)
