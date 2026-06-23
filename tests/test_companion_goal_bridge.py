"""Companion set-goal commands must reach the planner's observable intention.

Regression: ``tools/anima_steer.py set-goal`` sends ``{"cmd":"set_goal"}``
through the WebSocket and receives ``OK``, but the planner only consumes
``override_go_to`` and ``override_procedure``. The text is left stranded in
CommandBus, so ``data/state.json`` continues to show ``current_goal: (none)``.
"""

from types import SimpleNamespace

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry
from anima.web.command_bus import CommandBus


async def test_planner_consumes_companion_goal_into_current_goal() -> None:
    bus = CommandBus()
    planner = Planner(ProcedureRegistry(), command_bus=bus)
    ctx = SimpleNamespace(blackboard={})

    bus.set_goal("Mine near Minoc until the pack is heavy, then smelt at the forge.")

    result = await planner._handle_overrides(ctx)

    assert result is None
    assert ctx.blackboard["current_goal"] == {
        "description": "Mine near Minoc until the pack is heavy, then smelt at the forge.",
        "source": "companion",
    }
    assert ctx.blackboard["planner_intent"] == (
        "Companion goal: Mine near Minoc until the pack is heavy, then smelt at the forge."
    )


async def test_planner_consumes_companion_clear_goal() -> None:
    bus = CommandBus()
    planner = Planner(ProcedureRegistry(), command_bus=bus)
    ctx = SimpleNamespace(blackboard={
        "current_goal": {"description": "old", "source": "companion"},
        "planner_intent": "Companion goal: old",
    })

    bus.set_goal(None)

    result = await planner._handle_overrides(ctx)

    assert result is None
    assert "current_goal" not in ctx.blackboard
    assert "planner_intent" not in ctx.blackboard


def test_goal_update_is_consumed_once_and_distinguishes_clear_from_noop() -> None:
    bus = CommandBus()

    assert bus.consume_goal_update() == (False, None)

    bus.set_goal("mine safely")
    assert bus.consume_goal_update() == (True, "mine safely")
    assert bus.consume_goal_update() == (False, None)

    bus.set_goal(None)
    assert bus.consume_goal_update() == (True, None)
    assert bus.consume_goal_update() == (False, None)
