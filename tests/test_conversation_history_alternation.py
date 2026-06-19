"""Regression: bounded conversation history must keep opening on a ``user`` turn.

``record_conversation`` trims the history to ``MAX_CONVERSATION_HISTORY`` with a
plain front-slice ``del history[: len(history) - MAX]``.  Because a turn is two
messages (user + assistant), the running history is even-length at rest, and the
trim removes an *odd* number of leading messages the first time it fires — which
leaves the surviving window starting with an ``assistant`` message.  Anthropic (a
configured provider) rejects any request whose first non-system message is not a
``user`` turn, so the whole speech reply would 400 out.  The fix drops leading
non-``user`` turns after trimming; this test locks that invariant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.brain.prompt import (
    MAX_CONVERSATION_HISTORY,
    build_speech_messages,
    record_conversation,
)


def _ctx() -> SimpleNamespace:
    ss = SimpleNamespace(x=0, y=0, z=0, serial=0x1, gold=0, hits=0, hits_max=0)
    perception = SimpleNamespace(self_state=ss)
    return SimpleNamespace(blackboard={}, perception=perception)


def test_trimmed_history_starts_with_user_turn() -> None:
    ctx = _ctx()
    # Simulate many alternating turns so the bound trips with an odd-sized drop.
    turns = MAX_CONVERSATION_HISTORY  # well past the limit
    for i in range(turns):
        record_conversation(ctx, "user", f"player: msg {i}")
        record_conversation(ctx, "assistant", f"reply {i}")

    history = ctx.blackboard["conversation_history"]
    assert len(history) <= MAX_CONVERSATION_HISTORY
    # The invariant that matters for Anthropic: first kept message is a user turn.
    assert history[0]["role"] == "user"
    # And alternation is intact across the whole kept window.
    for prev, nxt in zip(history, history[1:]):
        assert prev["role"] != nxt["role"]


def test_build_speech_messages_first_turn_is_user() -> None:
    """The assembled prompt (after the system message) must open on ``user``."""
    ctx = _ctx()
    # persona absent → default system prompt; that's fine for this check.
    for i in range(MAX_CONVERSATION_HISTORY):
        record_conversation(ctx, "user", f"player: msg {i}")
        record_conversation(ctx, "assistant", f"reply {i}")

    messages = build_speech_messages(ctx, "player", "player: msg final")
    assert messages[0]["role"] == "system"
    # Everything after the system prompt must alternate starting from user.
    convo = messages[1:]
    assert convo, "expected at least one conversation turn"
    assert convo[0]["role"] == "user"
    for prev, nxt in zip(convo, convo[1:]):
        assert prev["role"] != nxt["role"]


def test_odd_leading_assistant_is_dropped() -> None:
    """Directly exercise the odd-drop case the plain front-slice would miss."""
    ctx = _ctx()
    history = ctx.blackboard.setdefault("conversation_history", [])
    # Pre-load exactly MAX messages of clean alternation.
    for i in range(MAX_CONVERSATION_HISTORY // 2):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    # One more user message overflows by 1 → naive trim drops the leading user,
    # leaving an assistant at the head. The fix must repair that.
    record_conversation(ctx, "user", "u_overflow")
    assert history[0]["role"] == "user"
