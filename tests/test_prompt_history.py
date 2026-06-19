"""Conversation-history assembly correctness for ``build_speech_messages``.

``respond_to_speech`` records the incoming player line into
``conversation_history`` (``record_conversation(ctx, "user", ...)``) *before*
it calls ``build_speech_messages``.  The builder must therefore NOT re-append
the same line: doing so duplicated the current user turn and emitted two
consecutive ``user`` messages, which breaks the strict user/assistant
alternation Anthropic requires and wastes prompt tokens.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.brain.prompt import (
    MAX_CONVERSATION_HISTORY,
    build_speech_messages,
    record_conversation,
)


def _make_ctx() -> SimpleNamespace:
    ss = SimpleNamespace(hits=50, hits_max=100, gold=0)
    perception = SimpleNamespace(self_state=ss)
    blackboard: dict = {}
    return SimpleNamespace(perception=perception, blackboard=blackboard)


def _no_consecutive_same_role(messages: list[dict[str, str]]) -> bool:
    convo = [m for m in messages if m["role"] != "system"]
    return all(
        convo[i]["role"] != convo[i + 1]["role"] for i in range(len(convo) - 1)
    )


def test_current_turn_recorded_first_is_not_duplicated():
    ctx = _make_ctx()
    speaker, text = "Bob", "hi there"

    # Caller records the incoming line first (mirrors respond_to_speech).
    record_conversation(ctx, "user", f"{speaker}: {text}")

    messages = build_speech_messages(ctx, speaker, text)

    user_lines = [
        m for m in messages
        if m["role"] == "user" and m["content"] == f"{speaker}: {text}"
    ]
    assert len(user_lines) == 1, f"current turn duplicated: {messages}"
    assert messages[-1] == {"role": "user", "content": f"{speaker}: {text}"}
    assert _no_consecutive_same_role(messages)


def test_alternation_preserved_over_multi_turn_history():
    ctx = _make_ctx()

    # Two completed turns already in history.
    record_conversation(ctx, "user", "Bob: hello")
    record_conversation(ctx, "assistant", "hey")
    record_conversation(ctx, "user", "Bob: how are you")
    record_conversation(ctx, "assistant", "fine")
    # New incoming line, recorded by the caller before building messages.
    record_conversation(ctx, "user", "Bob: where are you")

    messages = build_speech_messages(ctx, "Bob", "where are you")

    assert messages[0]["role"] == "system"
    convo = [m for m in messages if m["role"] != "system"]
    # First non-system message must be a user turn; roles strictly alternate.
    assert convo[0]["role"] == "user"
    assert _no_consecutive_same_role(messages)
    # The current turn appears exactly once and is the final message.
    assert messages[-1] == {"role": "user", "content": "Bob: where are you"}
    assert sum(1 for m in convo if m["content"] == "Bob: where are you") == 1


def test_appends_current_turn_when_history_missing_it():
    # Defensive path: if a caller forgot to record first, the current turn is
    # still appended so the model always sees the line it must answer.
    ctx = _make_ctx()
    messages = build_speech_messages(ctx, "Bob", "knock knock")
    assert messages[-1] == {"role": "user", "content": "Bob: knock knock"}
    assert _no_consecutive_same_role(messages)


def test_record_conversation_prunes_to_bound():
    ctx = _make_ctx()
    for i in range(MAX_CONVERSATION_HISTORY + 10):
        record_conversation(ctx, "user" if i % 2 == 0 else "assistant", f"m{i}")
    history = ctx.blackboard["conversation_history"]
    assert len(history) == MAX_CONVERSATION_HISTORY
    # Oldest entries dropped; newest retained.
    assert history[-1]["content"] == f"m{MAX_CONVERSATION_HISTORY + 9}"
