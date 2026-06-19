"""Robustness test: an empty / failed LLM think response must not burn the
whole THINK_COOLDOWN.

``LLMClient.chat`` returns ``LLMResponse(text="")`` on a timeout or any
transient backend error.  ``llm_think`` sets ``last_think_time = now`` *before*
the chat call, so without special handling an empty response would leave the
agent unable to think again for a full THINK_COOLDOWN (30s) — effectively
brain-dead on a flaky local backend.  The fix rolls ``last_think_time`` back so
the next tick retries after only THINK_RETRY_BACKOFF seconds.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.brain.think as think
from anima.brain.behavior_tree import Status
from anima.brain.llm import LLMResponse
from anima.brain.think import THINK_COOLDOWN, THINK_RETRY_BACKOFF, llm_think


def _make_ctx(chat_text: str) -> SimpleNamespace:
    ss = SimpleNamespace(
        x=100, y=100, z=0, serial=0x1,
        gold=0, weight=0, weight_max=0,
        hp_percent=100.0, hits=100, hits_max=100,
    )
    world = SimpleNamespace(
        nearby_items=lambda x, y, distance=0: [],
        nearby_mobiles=lambda x, y, distance=0: [],
    )
    social = SimpleNamespace(recent=lambda count=3: [])
    perception = SimpleNamespace(self_state=ss, world=world, social=social)

    llm = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text=chat_text, model="mock"))
    )

    # No active goal; skill state chosen so the pre-chat short-circuits don't fire.
    blackboard: dict = {
        "last_think_time": 0.0,       # old enough to pass the cooldown gate
        "last_player_speech": 0.0,    # not in conversation
        "skill_consecutive_fails": 0,
        "last_skill_time": 0.0,       # >10s ago → skill-success gate skipped
    }
    return SimpleNamespace(llm=llm, perception=perception, blackboard=blackboard)


@pytest.fixture(autouse=True)
def _patch_prompt_helpers(monkeypatch):
    """Stub the prompt/memory helpers so llm_think reaches the chat call cheaply."""
    monkeypatch.setattr(think, "retrieve_context", AsyncMock(return_value=""))
    monkeypatch.setattr(think, "build_system_prompt", lambda ctx, memory_block="": "sys")
    monkeypatch.setattr(think, "format_locations_for_llm", lambda x, y: "no places")


@pytest.mark.asyncio
async def test_empty_response_does_not_burn_full_cooldown(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    ctx = _make_ctx(chat_text="")  # simulate timeout / transient backend error
    status = await llm_think(ctx)

    assert status is Status.SUCCESS
    ctx.llm.chat.assert_awaited_once()

    # last_think_time must be rolled back so the NEXT tick can retry soon,
    # NOT pinned at `now` (which would block thinking for a full cooldown).
    last_think = ctx.blackboard["last_think_time"]
    elapsed_until_ready = THINK_COOLDOWN - (now - last_think)
    assert elapsed_until_ready == pytest.approx(THINK_RETRY_BACKOFF)
    assert elapsed_until_ready < THINK_COOLDOWN  # the whole point


@pytest.mark.asyncio
async def test_retry_fires_after_backoff_not_full_cooldown(monkeypatch):
    """After an empty response, a tick THINK_RETRY_BACKOFF seconds later must be
    allowed to call the LLM again (cooldown gate passes)."""
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(think.time, "time", lambda: t["now"])

    ctx = _make_ctx(chat_text="")
    await llm_think(ctx)
    first_calls = ctx.llm.chat.await_count
    assert first_calls == 1

    # Advance time by just past the short backoff (but far less than the cooldown).
    t["now"] += THINK_RETRY_BACKOFF + 0.5
    await llm_think(ctx)

    # The cooldown gate let us think again → the LLM was consulted a second time.
    assert ctx.llm.chat.await_count == 2


@pytest.mark.asyncio
async def test_successful_response_consumes_full_cooldown(monkeypatch):
    """Sanity: a normal (non-empty) response keeps the full cooldown so we don't
    accidentally spam the backend on the happy path."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    # An 'idle' action is a no-op decision that doesn't touch walker/conn.
    ctx = _make_ctx(chat_text='{"action": "idle", "say": ""}')
    status = await llm_think(ctx)

    assert status is Status.SUCCESS
    # Happy path leaves last_think_time at `now` → full cooldown before next think.
    assert ctx.blackboard["last_think_time"] == pytest.approx(now)
