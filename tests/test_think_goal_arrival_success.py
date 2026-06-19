"""Arriving at a goal must finish it as a SUCCESS exactly once.

Regression: the active-goal arrival branch in ``llm_think`` called
``_finish_goal(ctx, goal, "success")`` but did NOT ``return``. Control then
fell out of the ``if move_target is not None:`` block and reached the
``_finish_goal(ctx, goal, "failure")`` statement at the tail of the
``if goal:`` block, re-finishing the *same* just-completed goal as a FAILURE.
Every successful arrival was therefore logged/recorded as a failure and the
cleanup ran twice. These tests pin: arrival finishes the goal once, with
outcome "success", and the think tick returns SUCCESS without ever invoking
the LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.brain.think as think
from anima.brain.behavior_tree import Status
from anima.brain.llm import LLMResponse
from anima.brain.think import llm_think


def _make_ctx() -> SimpleNamespace:
    ss = SimpleNamespace(
        x=100, y=100, z=0, serial=0x1,
        gold=0, weight=0, weight_max=0, direction=0,
        hp_percent=100.0, hits=100, hits_max=100,
    )
    world = SimpleNamespace(
        nearby_items=lambda x, y, distance=0: [],
        nearby_mobiles=lambda x, y, distance=0: [],
    )
    social = SimpleNamespace(recent=lambda count=3: [])
    perception = SimpleNamespace(self_state=ss, world=world, social=social)

    llm = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text='{"action": "idle"}', model="mock"))
    )
    conn = SimpleNamespace(send_packet=AsyncMock())
    walker = SimpleNamespace(
        can_walk=lambda: True,
        denied_tiles={},
        consecutive_denials=0,
        check_stuck=lambda target: None,
    )
    blackboard: dict = {
        "last_think_time": 0.0,
        "skill_consecutive_fails": 0,
        "last_skill_time": 0.0,
    }
    return SimpleNamespace(
        llm=llm, perception=perception, blackboard=blackboard,
        conn=conn, memory_db=None, walker=walker, map_reader=object(), cfg=None,
    )


@pytest.fixture
def _finish_calls(monkeypatch):
    """Record every (place, outcome) passed to ``_finish_goal``."""
    calls: list[tuple[str, str]] = []

    def _spy(ctx, goal, outcome):
        calls.append((goal.get("place", "unknown"), outcome))
        ctx.blackboard.pop("current_goal", None)
        ctx.blackboard.pop("move_target", None)

    monkeypatch.setattr(think, "_finish_goal", _spy)
    return calls


@pytest.fixture(autouse=True)
def _patch_helpers(monkeypatch):
    monkeypatch.setattr(think, "retrieve_context", AsyncMock(return_value=""))
    monkeypatch.setattr(think, "build_system_prompt", lambda ctx, memory_block="": "sys")
    monkeypatch.setattr(think, "format_locations_for_llm", lambda x, y: "no places")
    monkeypatch.setattr(think, "_clear_path_cache", lambda ctx: None)
    monkeypatch.setattr(think, "_find_closed_door_at", lambda ctx, x, y: None)

    async def _fake_step(ctx, tx, ty):
        return Status.SUCCESS

    monkeypatch.setattr(think, "_step_toward", _fake_step)


@pytest.mark.asyncio
async def test_arrival_finishes_goal_once_as_success(monkeypatch, _finish_calls):
    """Standing on the goal tile finishes it as success — never as failure."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)
    # No separate inner/approach point, so the building-enter branch is skipped
    # and we land directly on the arrival-success path.
    monkeypatch.setattr(
        think, "find_location",
        lambda name, **kw: SimpleNamespace(
            name="Bank", nav_x=100, nav_y=100, x=100, y=100,
            approach_x=None, approach_y=None,
        ),
    )

    ctx = _make_ctx()
    # Agent is AT (100,100); goal target is the same tile → arrived.
    ctx.blackboard["current_goal"] = {
        "place": "Bank", "description": "to the bank", "x": 100, "y": 100,
    }
    ctx.blackboard["move_target"] = (100, 100)

    status = await llm_think(ctx)

    # Finished exactly once, as a success — the spurious failure re-finish is gone.
    assert _finish_calls == [("Bank", "success")]
    assert status is Status.SUCCESS
    # Arrival short-circuits before any fresh strategising — LLM never consulted.
    ctx.llm.chat.assert_not_awaited()
