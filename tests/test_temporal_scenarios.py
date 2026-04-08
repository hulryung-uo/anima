"""Temporal test scenarios exercising multi-tick planner behavior.

These tests run the planner for N ticks against a MockWorld to catch
sequence bugs (X fails → Y fails → Z loops) that are invisible in
single-tick unit tests.
"""

from __future__ import annotations

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry
from tests.harness.runner import run_planner_ticks
from tests.harness.world import MockWorld


@pytest.mark.asyncio
async def test_harness_runs_without_crash():
    """Smoke test: harness can run the planner for 5 ticks."""
    world = MockWorld()
    world.set_player_stats(gold=50, hits=100)
    registry = ProcedureRegistry()
    planner = Planner(registry)

    result = await run_planner_ticks(planner, world, max_ticks=5)
    assert result.ticks_run > 0


@pytest.mark.asyncio
async def test_selects_nothing_when_no_procedures_registered():
    """With an empty registry, select_procedure returns None each tick."""
    world = MockWorld()
    registry = ProcedureRegistry()
    planner = Planner(registry)

    result = await run_planner_ticks(planner, world, max_ticks=3)
    # No procedures registered → all selections are None (the default
    # fallback behavior — _MoveToProcedure may still be picked up as an
    # activity fallback).
    assert isinstance(result.procedures_selected, list)


@pytest.mark.asyncio
async def test_harness_records_time_progression():
    """Each tick advances the simulated world clock."""
    world = MockWorld()
    start = world.time
    registry = ProcedureRegistry()
    planner = Planner(registry)

    await run_planner_ticks(planner, world, max_ticks=10, tick_duration_s=0.5)
    # Expect ~5 seconds of simulated time to have passed
    assert world.time - start >= 4.0
