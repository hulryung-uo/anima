"""run_planner_ticks — drive the planner for N ticks against a MockWorld.

Does NOT call procedure.run() — outcomes are simulated deterministically
by a WorldBehavior so tests remain fast and fully reproducible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from anima.planner.planner import Planner
from tests.harness.world import MockWorld, WorldBehavior


@dataclass
class RunResult:
    """Summary of a completed harness run."""

    ticks_run: int
    procedures_selected: list[str]
    procedures_succeeded: list[str]
    procedures_failed: list[str]
    final_state: dict

    def count(self, procedure: str) -> int:
        """How many times *procedure* was selected during the run."""
        return self.procedures_selected.count(procedure)


async def run_planner_ticks(
    planner: Planner,
    world: MockWorld,
    max_ticks: int = 100,
    stop_when: Callable[[MockWorld], bool] | None = None,
    tick_duration_s: float = 0.2,
    behavior: WorldBehavior | None = None,
) -> RunResult:
    """Run the planner against a mocked world for up to *max_ticks*.

    Each iteration:
    1. Build an AgentContext-shaped mock via ``world.as_ctx()``.
    2. Call ``planner.select_procedure(ctx)`` — record the choice.
    3. Apply a simulated outcome via ``WorldBehavior.on_procedure_selected``.
    4. Advance ``world.time`` by *tick_duration_s*.
    5. Check *stop_when* — bail early if it returns True.

    Procedure ``.run()`` is **never** called, so there is no packet I/O,
    no real asyncio timers, and no side-effects outside the MockWorld.

    Parameters
    ----------
    planner:
        A ``Planner`` instance (may have an empty or populated registry).
    world:
        The ``MockWorld`` to drive.
    max_ticks:
        Maximum number of planner calls before returning.
    stop_when:
        Optional callable taking the current ``MockWorld`` and returning
        ``True`` to stop the run early.
    tick_duration_s:
        How many simulated seconds each tick represents (advances
        ``world.time``).
    behavior:
        Strategy object controlling simulated outcomes.  Defaults to the
        base ``WorldBehavior`` (most things succeed).
    """
    if behavior is None:
        behavior = WorldBehavior()

    selected: list[str] = []
    succeeded: list[str] = []
    failed: list[str] = []

    for tick in range(max_ticks):
        # Rebuild ctx each tick so self_state reflects latest world mutations
        ctx = world.as_ctx()

        # Ask the planner what to do
        try:
            proc = await planner.select_procedure(ctx)
        except Exception:
            # Planner errors should not abort the harness — record as None
            proc = None

        proc_name = proc.name if proc is not None else "<none>"

        # Log to world event stream
        world._record_procedure_selected(proc_name)
        selected.append(proc_name)

        # Apply simulated outcome (skip for None / no-op tick)
        if proc is not None:
            try:
                success, reason = behavior.on_procedure_selected(world, proc_name)
            except Exception:
                success, reason = False, "behavior_error"

            if success:
                succeeded.append(proc_name)
            else:
                failed.append(proc_name)

        # Advance simulated clock
        world.advance_time(tick_duration_s)

        # Early-exit hook
        if stop_when is not None and stop_when(world):
            break

    final_state = {
        "time": world.time,
        "gold": world._gold,
        "hits": world._hits,
        "weight": world._weight,
        "player_x": world.player_x,
        "player_y": world.player_y,
        "items": len(world._items),
    }

    return RunResult(
        ticks_run=len(selected),
        procedures_selected=selected,
        procedures_succeeded=succeeded,
        procedures_failed=failed,
        final_state=final_state,
    )
