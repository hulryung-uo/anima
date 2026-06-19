"""A goal whose move_target was lost (cleared by a transient pathfinding
failure) must be restored from the goal's OWN stored coordinates, not by
re-resolving the place NAME through the static location table.

Every ``current_goal`` dict carries an authoritative ``x``/``y`` (stamped from
``loc.nav_x``/``nav_y`` when the goal is set, both in ``think.llm_think`` and
in ``core/goals.py``). The restore path used to ignore those and call
``find_location(goal["place"])``. When the place name does not round-trip
through ``ALL_LOCATIONS`` (a discovered/dynamic destination, a planner-set
goal, or any name-normalization mismatch) ``find_location`` returns ``None``,
``move_target`` stays ``None``, and the goal is abandoned as a *failure* even
though it carried walkable coordinates. These tests pin the new contract:
stored coords win; name lookup is only a fallback.
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
        check_stuck=lambda target: None,  # not stuck
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


@pytest.fixture(autouse=True)
def _patch_helpers(monkeypatch):
    monkeypatch.setattr(think, "retrieve_context", AsyncMock(return_value=""))
    monkeypatch.setattr(think, "build_system_prompt", lambda ctx, memory_block="": "sys")
    monkeypatch.setattr(think, "format_locations_for_llm", lambda x, y: "no places")
    monkeypatch.setattr(think, "_clear_path_cache", lambda ctx: None)
    monkeypatch.setattr(think, "_find_closed_door_at", lambda ctx, x, y: None)
    # Capture the destination the mover was actually sent toward.
    steps: list[tuple[int, int]] = []

    async def _fake_step(ctx, tx, ty):
        steps.append((tx, ty))
        return Status.SUCCESS

    monkeypatch.setattr(think, "_step_toward", _fake_step)
    return steps


@pytest.mark.asyncio
async def test_lost_move_target_restores_from_goal_coords_not_name(monkeypatch, _patch_helpers):
    """find_location can't resolve the place name, but the goal's own (x,y)
    must restore the walk target — the goal is NOT abandoned."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)
    # find_location returns None for the (dynamic / un-tabled) place name.
    monkeypatch.setattr(think, "find_location", lambda name: None)

    ctx = _make_ctx()
    ctx.blackboard["current_goal"] = {
        "place": "Discovered Cave Entrance",
        "description": "to the cave",
        "x": 250,
        "y": 300,
    }
    # move_target was cleared (e.g. by a transient pathfinding failure).
    ctx.blackboard["move_target"] = None

    status = await llm_think(ctx)

    # Goal survived, move_target restored from the stored coords, and the mover
    # was driven toward exactly those coords (not abandoned, no fresh think).
    assert ctx.blackboard.get("current_goal") is not None
    assert ctx.blackboard.get("move_target") == (250, 300)
    assert _patch_helpers == [(250, 300)]
    ctx.llm.chat.assert_not_awaited()
    assert status is Status.SUCCESS


@pytest.mark.asyncio
async def test_goal_without_coords_still_falls_back_to_name_lookup(monkeypatch, _patch_helpers):
    """A legacy goal dict lacking x/y must still be restorable via the name
    lookup (fallback preserved)."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)
    monkeypatch.setattr(
        think, "find_location",
        lambda name: SimpleNamespace(name="Bank", nav_x=400, nav_y=410),
    )

    ctx = _make_ctx()
    ctx.blackboard["current_goal"] = {"place": "bank", "description": "to the bank"}
    ctx.blackboard["move_target"] = None

    status = await llm_think(ctx)

    assert ctx.blackboard.get("move_target") == (400, 410)
    assert _patch_helpers == [(400, 410)]
    assert status is Status.SUCCESS
