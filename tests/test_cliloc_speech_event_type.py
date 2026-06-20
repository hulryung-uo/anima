"""Regression: cliloc (0xC1 / 0xCC) SPEECH_HEARD events must carry ``type``.

The 0x1C ASCII-talk and 0xAE Unicode-talk handlers forward the on-wire
``msg_type`` in the emitted ``SPEECH_HEARD`` event, but the two localized-message
handlers (0xC1 ClilocMessage, 0xCC SendLocalizedMessageAffix) emitted only
``{serial, name, text}`` — dropping the type.

Both consumers of the event classify a line as conversational vs. not via
``data.get("type", 0)``, and a MISSING key defaults to 0 == MessageType.REGULAR.
So a SYSTEM cliloc notice (or a LABEL) routed through 0xC1/0xCC read as a real
person talking: Brain._poll_events queued a bogus pending reply AND stamped
``last_player_speech``, which makes llm_think treat the agent as "in conversation"
and freezes fresh strategising for the whole CONVERSATION_TIMEOUT window — and
respond_to_speech could speak aloud at a server line (a bot tell). ServUO
delivers many gameplay notices via these exact packets, so this fired constantly.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

from anima.brain.brain import Brain
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.enums import MessageType
from anima.perception.event_stream import GameEventType
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

# cliloc 3000201 = "You must wait to perform another action." — a no-arg ServUO
# system message commonly delivered via the localized-message packets.
_CLILOC = 3000201
_AFFIX_SYSTEM = 0x02


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _cliloc_packet(serial: int, cliloc: int, msg_type: int, name: str) -> bytes:
    """Build a 0xC1 ClilocMessage packet (no UTF-16 args).

    Layout: [0xC1][len:u16][serial:u32][graphic:u16][msg_type:u8][hue:u16]
            [font:u16][cliloc:u32][name:ascii30][args:utf16-le NUL]
    """
    body = b""
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0)  # graphic
    body += struct.pack(">B", msg_type)
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += struct.pack(">I", cliloc)
    name_bytes = name.encode("ascii")[:30]
    body += name_bytes + b"\x00" * (30 - len(name_bytes))
    body += b"\x00\x00"  # empty UTF-16-LE args (NUL terminator)
    full = struct.pack(">B", 0xC1) + struct.pack(">H", 0) + body
    return full[:1] + struct.pack(">H", len(full)) + full[3:]


def _affix_packet(serial: int, cliloc: int, flags: int, name: str) -> bytes:
    """Build a 0xCC SendLocalizedMessageAffix packet (no UTF-16 args)."""
    body = b""
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0)  # graphic
    body += struct.pack(">B", 0)  # wire msg_type (REGULAR; System flag overrides)
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += struct.pack(">I", cliloc)
    body += struct.pack(">B", flags)
    name_bytes = name.encode("ascii")[:30]
    body += name_bytes + b"\x00" * (30 - len(name_bytes))
    body += b"\x00"  # empty NUL-terminated affix
    full = struct.pack(">B", 0xCC) + struct.pack(">H", 0) + body
    return full[:1] + struct.pack(">H", len(full)) + full[3:]


# A SYSTEM cliloc line is attributed to a MOBILE-range serial (< 0x40000000),
# so the item-serial guard never catches it — only the type filter can.
_MOBILE_SERIAL = 0x00012345


def test_c1_cliloc_emits_event_type() -> None:
    h, p, _ = _make_stack()
    h.dispatch(0xC1, _cliloc_packet(_MOBILE_SERIAL, _CLILOC, int(MessageType.SYSTEM), "Britain"))
    events = [e for e in p.poll_events() if e.type == GameEventType.SPEECH_HEARD]
    assert events, "0xC1 must emit a SPEECH_HEARD event"
    # The bug: this key was absent, so consumers defaulted it to REGULAR.
    assert events[-1].data.get("type") == int(MessageType.SYSTEM)


def test_cc_affix_system_emits_event_type() -> None:
    h, p, _ = _make_stack()
    h.dispatch(0xCC, _affix_packet(_MOBILE_SERIAL, _CLILOC, _AFFIX_SYSTEM, "System"))
    events = [e for e in p.poll_events() if e.type == GameEventType.SPEECH_HEARD]
    assert events, "0xCC must emit a SPEECH_HEARD event"
    # The System affix forces SYSTEM; that must survive into the event.
    assert int(events[-1].data.get("type")) == int(MessageType.SYSTEM)


def _brain_for(perception: Perception) -> Brain:
    ctx = SimpleNamespace(perception=perception, blackboard={})
    return Brain(ctx, root=SimpleNamespace())


def test_system_cliloc_does_not_freeze_thinking() -> None:
    """End-to-end: a SYSTEM cliloc line must NOT queue a reply or stamp the
    conversation clock once its type is forwarded.  This is the behaviour the
    dropped ``type`` key silently broke."""
    h, p, _ = _make_stack()
    h.dispatch(0xC1, _cliloc_packet(_MOBILE_SERIAL, _CLILOC, int(MessageType.SYSTEM), "Britain"))

    # Re-route the brain's perception poll to drain this perception's events.
    perception = SimpleNamespace(
        self_state=SimpleNamespace(serial=0x00000001),
        poll_events=p.poll_events,
    )
    brain = _brain_for(perception)
    brain._poll_events()

    bb = brain.context.blackboard
    assert not bb.get("pending_speech"), (
        "a SYSTEM cliloc line must not queue as pending speech"
    )
    assert "last_player_speech" not in bb, (
        "a SYSTEM cliloc line must not start a conversation that freezes thinking"
    )


def test_regular_cliloc_still_counts_as_conversation() -> None:
    """Guard against over-reach: a REGULAR-typed cliloc line from a mobile must
    still queue and stamp the clock — the fix only filters by the real type."""
    h, p, _ = _make_stack()
    h.dispatch(0xC1, _cliloc_packet(_MOBILE_SERIAL, _CLILOC, int(MessageType.REGULAR), "Hastin"))

    perception = SimpleNamespace(
        self_state=SimpleNamespace(serial=0x00000001),
        poll_events=p.poll_events,
    )
    brain = _brain_for(perception)
    brain._poll_events()

    bb = brain.context.blackboard
    assert bb.get("pending_speech"), "a REGULAR cliloc line is real chatter and must queue"
    assert isinstance(bb.get("last_player_speech"), float)
