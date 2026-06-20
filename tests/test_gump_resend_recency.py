"""A re-sent gump (same TYPE id) must read as the NEWEST arrival.

Regression: the 0xB0 / 0xDD open-gump handlers stored the parsed gump with a
plain ``ss.gumps[gump_id] = gump``. Python dict assignment to an ALREADY-present
key overwrites the value but keeps the key's ORIGINAL insertion position, so a
re-sent/refreshed gump (craft gumps refresh under a fixed type id after every
click) did not move to the end of the dict. ``wait_for_gump`` picks the
most-recently-inserted key as "newest", so with another gump still on screen it
handed back the STALE window and ``craft_via_gump`` clicked the wrong gump —
tripping ServUO's "Invalid gump response, disconnecting..." path or driving the
wrong window.

This drives the REAL packet handler (not a hand-built dict) so the
insertion-order fix is exercised end to end through ``wait_for_gump``.
"""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from anima.actions.gump import wait_for_gump
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _open_gump_packet(serial: int, gump_id: int, layout: str = "{ button 0 0 1 2 1 0 5 }") -> bytes:
    """Build a 0xB0 OpenGump packet: serial, gump_id, x, y, layout, 0 text lines."""
    body = bytearray()
    body += struct.pack(">I", serial)
    body += struct.pack(">I", gump_id)
    body += struct.pack(">I", 0)  # x
    body += struct.pack(">I", 0)  # y
    lb = layout.encode("ascii")
    body += struct.pack(">H", len(lb))
    body += lb
    body += struct.pack(">H", 0)  # text-line count
    pkt = bytearray()
    pkt.append(0xB0)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def _ctx(perception: Perception) -> SimpleNamespace:
    # bus=None routes wait_for_gump through its poll fallback, which returns
    # immediately because a gump is already present.
    return SimpleNamespace(perception=perception, bus=None)


def test_resent_gump_becomes_the_newest_arrival():
    h, p = _make_stack()

    # Craft gump (type id 100) opens, then an unrelated gump (200) opens, then
    # the craft gump REFRESHES under the same type id 100 with a new serial.
    h.dispatch(0xB0, _open_gump_packet(serial=0x10, gump_id=100))
    h.dispatch(0xB0, _open_gump_packet(serial=0x20, gump_id=200))
    h.dispatch(0xB0, _open_gump_packet(serial=0x11, gump_id=100))

    # Both ids are still present (count unchanged) ...
    assert set(p.self_state.gumps) == {100, 200}
    # ... but the just-refreshed gump (100) must now read as the most recent.
    assert list(p.self_state.gumps)[-1] == 100
    assert p.self_state.gumps[100].serial == 0x11  # value really was refreshed

    result = asyncio.run(wait_for_gump(_ctx(p), timeout=0.1))
    assert result.success
    # Before the fix this returned 200 (the stale window) because 100 kept its
    # original earlier slot.
    assert result.data["gump_id"] == 100
    assert result.data["gump"] is p.self_state.gumps[100]


def test_distinct_new_gump_still_reads_as_newest():
    """A genuinely new type id must still land last (no regression)."""
    h, p = _make_stack()
    h.dispatch(0xB0, _open_gump_packet(serial=0x10, gump_id=100))
    h.dispatch(0xB0, _open_gump_packet(serial=0x20, gump_id=200))

    result = asyncio.run(wait_for_gump(_ctx(p), timeout=0.1))
    assert result.success
    assert result.data["gump_id"] == 200
