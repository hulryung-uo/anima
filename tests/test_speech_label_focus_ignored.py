"""Regression: the agent must not reply aloud to LABEL / FOCUS speech lines.

Single-click name responses (msg_type 6 = LABEL, e.g. "Hastin the baker") and
FOCUS prompts (msg_type 7) ride the same 0x1C/0x1D/0xC1/0xCC speech packets a
real utterance does and surface as SPEECH_HEARD events carrying a MOBILE-range
serial (< 0x40000000). The old guard only rejected MessageType.SYSTEM, so a
LABEL/FOCUS line slipped past the item-serial check and drew a spoken reply —
the agent talking out loud to a name tag, an obvious bot tell.
think._build_recent_speech already excludes this exact set from the LLM
conversation window; respond_to_speech must reject it too.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from anima.action.speech import respond_to_speech
from anima.brain.behavior_tree import Status
from anima.perception.enums import MessageType


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_packet(self, data: bytes) -> None:
        self.sent.append(data)


def _make_ctx(speech: dict) -> SimpleNamespace:
    """Minimal BrainContext stand-in: no LLM / memory_db, so only the guard
    and tier-1 paths run and nothing touches the network beyond _FakeConn."""
    self_state = SimpleNamespace(serial=0x00000001, x=100, y=200)
    perception = SimpleNamespace(self_state=self_state)
    blackboard: dict = {"pending_speech": [speech], "persona": None}
    return SimpleNamespace(
        blackboard=blackboard,
        perception=perception,
        conn=_FakeConn(),
        llm=None,
        memory_db=None,
    )


def _run(ctx) -> Status:
    return asyncio.run(respond_to_speech(ctx))


# A mobile-range serial (< 0x40000000): an NPC/object the label/focus line is
# attributed to. This is what makes the bug bite — the item-serial guard does
# not catch it.
MOBILE_SERIAL = 0x00012345


def test_label_line_draws_no_reply() -> None:
    # A single-click name label ("Hastin the baker") even worded like a
    # greeting must not be answered.
    ctx = _make_ctx(
        {
            "serial": MOBILE_SERIAL,
            "name": "Hastin",
            "text": "Hello!",
            "type": MessageType.LABEL,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


def test_focus_prompt_draws_no_reply() -> None:
    ctx = _make_ctx(
        {
            "serial": MOBILE_SERIAL,
            "name": "a guard",
            "text": "Hello!",
            "type": MessageType.FOCUS,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


def test_regular_speech_still_replies() -> None:
    # Guard against over-filtering: a genuine REGULAR utterance from the same
    # mobile-range serial must still get a tier-1 greeting reply on the wire.
    ctx = _make_ctx(
        {
            "serial": MOBILE_SERIAL,
            "name": "Aelius",
            "text": "Hello!",
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.SUCCESS
    assert len(ctx.conn.sent) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
