"""Regression: the deterministic speech fallback must match the speaker's language.

``respond_to_speech`` has three reply tiers: (1) a pattern-matched greeting,
(2) an LLM response, and (3) a hard-coded fallback used when no LLM is wired up
or the LLM returned no text. Tier 1 already localizes (GREETING_RESPONSES_KR),
but the tier-3 fallback unconditionally emitted English "I heard you, <name>.".

The persona contract is strict: "Reply in the SAME language spoken to you.
Korean -> Korean only." A Korean player whose (non-greeting) line reached the
fallback therefore got an English reply — an immersion break that reads exactly
like a bot whose model fell over. This pins the fallback to the speaker's
language so the fix can't silently regress.
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
    # No LLM and no memory_db: tier-2 is skipped entirely, so a non-greeting
    # line lands in the deterministic tier-3 fallback — the path under test.
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


PLAYER_SERIAL = 0x00012345


def _sent_text_utf16(packet: bytes) -> str:
    """Decode the UTF-16-BE body of a plain (no-keyword) 0xAD speech packet.

    Layout: [0xAD][len u16][type u8][hue u16][font u16][lang 4][text utf16][0000].
    The fallback strings carry no keyword, so they are framed in plain mode.
    """
    assert packet[0] == 0xAD
    body = packet[12:]  # skip header (1+2+1+2+2+4) bytes through the 4-byte lang
    return body.decode("utf-16-be", errors="ignore").rstrip("\x00")


def test_korean_nongreeting_fallback_is_korean() -> None:
    # A Korean line that is NOT a tier-1 greeting -> reaches the fallback.
    ctx = _make_ctx(
        {
            "serial": PLAYER_SERIAL,
            "name": "철수",
            "text": "철 좀 팔아?",  # "you selling iron?" — no greeting token
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.SUCCESS
    assert len(ctx.conn.sent) == 1

    text = _sent_text_utf16(ctx.conn.sent[0])
    # The reply must contain Hangul and must NOT be the English fallback.
    assert any("가" <= c <= "힣" for c in text), text
    assert "I heard you" not in text


def test_english_nongreeting_fallback_is_english() -> None:
    ctx = _make_ctx(
        {
            "serial": PLAYER_SERIAL,
            "name": "Aelius",
            "text": "selling iron ingots cheap",  # non-greeting -> fallback
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.SUCCESS
    assert len(ctx.conn.sent) == 1

    text = _sent_text_utf16(ctx.conn.sent[0])
    assert "I heard you" in text
    # English fallback must not accidentally carry Hangul.
    assert not any("가" <= c <= "힣" for c in text), text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
