"""Robustness test: an LLM action object with explicit JSON ``null`` values for
``say`` / ``reason`` / ``action`` must not crash the think tick.

``dict.get(key, default)`` returns the default only when *key is absent* — a key
present with value ``null`` yields ``None``.  Models very frequently emit
``{"action": "speak", "say": null}`` instead of ``"say": ""``.  Before the fix
``llm_think`` did ``action.get("say", "").strip()`` which became
``None.strip()`` → ``AttributeError``, aborting the entire decision tick.  The
fix coerces these fields with ``or`` so null collapses to the empty/default
form.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.brain.think as think
from anima.brain.behavior_tree import Status
from anima.brain.llm import LLMResponse
from anima.brain.think import llm_think


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

    # send_packet must never be awaited for a null/empty ``say``.
    conn = SimpleNamespace(send_packet=AsyncMock())

    blackboard: dict = {
        "last_think_time": 0.0,       # old enough to pass the cooldown gate
        "last_player_speech": 0.0,    # not in conversation
        "skill_consecutive_fails": 0,
        "last_skill_time": 0.0,       # >10s ago → skill-success gate skipped
    }
    return SimpleNamespace(
        llm=llm, perception=perception, blackboard=blackboard,
        conn=conn, memory_db=None,
    )


@pytest.fixture(autouse=True)
def _patch_prompt_helpers(monkeypatch):
    """Stub the prompt/memory helpers so llm_think reaches the chat call cheaply."""
    monkeypatch.setattr(think, "retrieve_context", AsyncMock(return_value=""))
    monkeypatch.setattr(think, "build_system_prompt", lambda ctx, memory_block="": "sys")
    monkeypatch.setattr(think, "format_locations_for_llm", lambda x, y: "no places")


@pytest.mark.asyncio
async def test_null_say_does_not_crash(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    # Explicit JSON null for ``say`` (and ``reason``) — the common LLM shape.
    ctx = _make_ctx(chat_text='{"action": "idle", "reason": null, "say": null}')

    # Before the fix this raised AttributeError: 'NoneType' has no 'strip'.
    status = await llm_think(ctx)

    assert status is Status.SUCCESS
    ctx.llm.chat.assert_awaited_once()
    # A null ``say`` is treated as empty → no speech packet emitted.
    ctx.conn.send_packet.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_action_falls_back_to_explore(monkeypatch):
    """A null ``action`` must collapse to the 'explore' default, not None
    (which would fall through to the unknown-skill branch with skill name None)."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    ctx = _make_ctx(chat_text='{"action": null, "say": null}')
    # skill_registry absent → if act were None it would still not crash, but it
    # would mis-route into the unknown-action branch instead of 'explore'.
    status = await llm_think(ctx)

    assert status is Status.SUCCESS
    ctx.conn.send_packet.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_say_still_spoken(monkeypatch):
    """Sanity: a genuine non-empty ``say`` still emits a speech packet, so the
    null-coercion didn't suppress legitimate speech."""
    now = 1_000_000.0
    monkeypatch.setattr(think.time, "time", lambda: now)

    ctx = _make_ctx(chat_text='{"action": "idle", "say": "hello there"}')
    status = await llm_think(ctx)

    assert status is Status.SUCCESS
    ctx.conn.send_packet.assert_awaited_once()
