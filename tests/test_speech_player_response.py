"""Regression test: the agent must actually respond to player speech.

commit 1c9be35 added a `serial < 0x40000000` guard meant to skip NPC chatter,
but in UO that boundary separates MOBILES (players *and* NPCs, all < 0x40000000)
from ITEMS (>= 0x40000000). The guard was inverted and silently dropped every
player's speech, so the agent never greeted or replied to anyone. This test
pins the corrected behavior: a real mobile speaker gets a tier-1 greeting reply,
while item-range serials and SYSTEM-type lines are still ignored.
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
    """Minimal BrainContext stand-in for respond_to_speech.

    No LLM and no memory_db so we exercise the tier-1 / guard logic only and
    never touch the network beyond the in-memory _FakeConn.
    """
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


# A typical live player on a ServUO shard: mobile serial well below the
# 0x40000000 item boundary, REGULAR speech type.
PLAYER_SERIAL = 0x00012345


def test_player_greeting_gets_a_reply() -> None:
    ctx = _make_ctx(
        {
            "serial": PLAYER_SERIAL,
            "name": "Aelius",
            "text": "Hello!",
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.SUCCESS
    # Tier-1 greeting actually went out on the wire.
    assert len(ctx.conn.sent) == 1


def test_player_speech_is_not_dropped_by_serial_range() -> None:
    # The whole point of the bug: a player serial (< 0x40000000) must NOT be
    # treated as a non-person to ignore.
    ctx = _make_ctx(
        {
            "serial": PLAYER_SERIAL,
            "name": "Aelius",
            "text": "selling iron ingots cheap",  # not a greeting -> tier 2
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    # No LLM configured, so it falls through to the deterministic fallback
    # reply rather than FAILURE. The key assertion is that it did NOT bail out.
    assert status is Status.SUCCESS
    assert len(ctx.conn.sent) == 1


def test_item_range_serial_is_ignored() -> None:
    # Items / multis live at >= 0x40000000 and can never be a person talking.
    ctx = _make_ctx(
        {
            "serial": 0x40000123,
            "name": "a sign",
            "text": "Hello!",
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


def test_system_type_speech_is_ignored() -> None:
    # Server-emitted SYSTEM lines routed through the speech path are not
    # conversation and must not draw a reply, even from a mobile-range serial.
    ctx = _make_ctx(
        {
            "serial": PLAYER_SERIAL,
            "name": "Aelius",
            "text": "Hello!",
            "type": MessageType.SYSTEM,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


def test_own_speech_is_ignored() -> None:
    ctx = _make_ctx(
        {
            "serial": 0x00000001,  # == self_state.serial
            "name": "Anima",
            "text": "Hello!",
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


def test_broadcast_serial_is_ignored() -> None:
    ctx = _make_ctx(
        {
            "serial": 0xFFFFFFFF,
            "name": "someone",
            "text": "Hello!",
            "type": MessageType.REGULAR,
        }
    )
    status = _run(ctx)
    assert status is Status.FAILURE
    assert ctx.conn.sent == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
