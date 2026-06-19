"""0xD6 MegaCliloc arg decode — feed crafted bytes, verify the OPL name/props.

Regression: the 0xD6 handler decoded each property's UTF-16 arg blob inline with
``raw.decode("utf-16-le", errors="replace")``, which diverges from ClassicUO's
``ReadUnicodeLE(length/2)`` (PacketHandlers.cs:4947) on two counts:

  1. an odd ``text_len`` leaves a trailing U+FFFD instead of being floored away, and
  2. an embedded NUL terminator does NOT cut the string, so post-terminator padding /
     stale bytes leak into the decoded arg.

Because MegaCliloc is anima's only mobile-name source, that garbage became
``mob.name`` / ``opl_names[serial]`` (e.g. ``"Healer\\x00GHOST"``), so every
name-keyed gate (combat target id, social greeting, NPC lookup) saw a name that no
longer compared equal to the real one. The fix routes 0xD6 through the shared
``_decode_cliloc_args`` helper that 0xC1/0xCC already use.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_mega_cliloc(serial: int, entries: list[tuple[int, bytes]]) -> bytes:
    """Build a 0xD6 MegaCliloc packet.

    Each entry is (cliloc_num, raw_utf16le_arg_bytes); the raw bytes are written
    verbatim with their byte length so a test can embed NUL terminators / padding.
    """
    body = bytearray()
    body += struct.pack(">H", 0x0001)  # unknown
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0x0000)  # unknown
    body += struct.pack(">I", 0x11223344)  # revision / hash
    for cliloc_num, raw in entries:
        body += struct.pack(">I", cliloc_num)
        body += struct.pack(">H", len(raw))
        body += raw
    body += struct.pack(">I", 0)  # cliloc 0 terminator
    pkt = bytearray()
    pkt.append(0xD6)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_mega_cliloc_arg_stops_at_embedded_nul():
    """An arg with an embedded NUL + trailing garbage must cut at the NUL.

    cliloc 9999999 has no base text, so the decoded arg lands directly as the
    property (and, being the first property, as the mobile's OPL name).
    """
    h, p, _ = _make_stack()
    serial = 0x00012345
    p.world.get_or_create_mobile(serial)

    # "Healer" + NUL terminator + post-terminator garbage that must NOT leak.
    raw = "Healer".encode("utf-16-le") + b"\x00\x00" + "GHOST".encode("utf-16-le")
    h.dispatch(0xD6, _build_mega_cliloc(serial, [(9999999, raw)]))

    assert p.world.mobiles[serial].name == "Healer"
    assert p.world.opl_names[serial] == "Healer"
    assert p.world.opl_properties[serial] == ["Healer"]
    # The specific regression value the buggy inline decode produced.
    assert "\x00" not in p.world.mobiles[serial].name
    assert "GHOST" not in p.world.mobiles[serial].name


def test_mega_cliloc_arg_floors_odd_length():
    """An odd byte length must be floored (no trailing U+FFFD), like ReadUnicodeLE.

    ClassicUO reads ``length / 2`` whole code units, dropping a stray odd byte;
    the old ``utf-16-le`` decode turned it into a replacement char instead.
    """
    h, p, _ = _make_stack()
    serial = 0x00067890
    p.world.get_or_create_mobile(serial)

    raw = "Orc".encode("utf-16-le") + b"\x7a"  # 6 bytes + 1 stray odd byte
    h.dispatch(0xD6, _build_mega_cliloc(serial, [(9999999, raw)]))

    assert p.world.mobiles[serial].name == "Orc"
    assert "�" not in p.world.mobiles[serial].name
