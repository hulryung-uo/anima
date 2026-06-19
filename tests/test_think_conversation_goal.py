"""An active conversation must not freeze a walk already in progress.

``llm_think`` pauses *fresh* strategising while someone is talking to us so the
agent doesn't wander off or re-decide mid-conversation. That gate used to sit
ahead of the active-goal movement block, so any nearby utterance — including a
hostile mob's speech, which also stamps ``last_player_speech`` — stranded the
agent in place for the whole CONVERSATION_TIMEOUT window even though it had a
destination it was already walking toward.

The fix keeps the conversation pause for the *new-think* path only; an active
goal keeps stepping. These tests pin both halves of that contract.
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

    # chat must NOT fire while a goal is being pursued — pursuit is deterministic.
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
    # Sentinel for the walk step so we don't need a real map_reader / pathfinder.
    monkeypatch.setattr(think, "_step_toward", AsyncMock(return_value=Status.SUCCESS))
    monkeypatch.setattr(think, "_clear_path_cache", lambda ctx: None)
    monkeypatch.setattr(think, "_find_closed_door_at", lambda ctx, x, y: None)


@pytest.mark.asyncio
async def test_active_goal_keeps_walking_during_conversation(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    ctx = _make_ctx()
    # Someone spoke to us 1s ago → in_conversation is True, no pending reply.
    ctx.blackboard["last_player_speech"] = now - 1.0
    # An active goal with a far-off target the agent is en route to.
    ctx.blackboard["current_goal"] = {"place": "bank", "description": "to the bank"}
    ctx.blackboard["move_target"] = (200, 200)

    status = await llm_think(ctx)

    # The walk must continue: _step_toward was taken and no fresh think happened.
    think._step_toward.assert_awaited_once()
    ctx.llm.chat.assert_not_awaited()
    assert status is Status.SUCCESS


@pytest.mark.asyncio
async def test_conversation_still_blocks_fresh_think_with_no_goal(monkeypatch):
    """The other half of the contract: with no active goal, an open
    conversation still suppresses a brand-new think (no LLM call, no walk)."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    ctx = _make_ctx()
    ctx.blackboard["last_player_speech"] = now - 1.0  # in conversation
    # No current_goal / move_target.

    status = await llm_think(ctx)

    ctx.llm.chat.assert_not_awaited()
    think._step_toward.assert_not_awaited()
    assert status is Status.SUCCESS
