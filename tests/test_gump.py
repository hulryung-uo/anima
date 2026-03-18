"""Tests for gump parsing, packet handling, and response building."""

import struct
import zlib

from anima.client.codec import PacketReader, PacketWriter
from anima.client.handler import PacketHandler
from anima.client.packets import build_gump_response
from anima.perception import Perception
from anima.perception.event_stream import GameEventType
from anima.perception.gump import parse_layout
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


# ---------------------------------------------------------------------------
# Layout parser
# ---------------------------------------------------------------------------


class TestParseLayout:
    def test_empty_layout(self):
        gump = parse_layout("", [])
        assert gump.buttons == []
        assert gump.texts == []

    def test_button(self):
        layout = "{ button 10 20 4005 4007 1 0 42 }"
        gump = parse_layout(layout, [])
        assert len(gump.buttons) == 1
        btn = gump.buttons[0]
        assert btn.x == 10
        assert btn.y == 20
        assert btn.normal_id == 4005
        assert btn.pressed_id == 4007
        assert btn.button_type == 1
        assert btn.param == 0
        assert btn.button_id == 42

    def test_page_button(self):
        layout = "{ button 10 20 4005 4007 0 3 0 }"
        gump = parse_layout(layout, [])
        btn = gump.buttons[0]
        assert btn.button_type == 0
        assert btn.param == 3

    def test_text(self):
        layout = "{ text 50 100 1152 0 }"
        lines = ["Hello World"]
        gump = parse_layout(layout, lines)
        assert len(gump.texts) == 1
        t = gump.texts[0]
        assert t.x == 50
        assert t.y == 100
        assert t.hue == 1152
        assert t.text_id == 0
        assert gump.get_text(0) == "Hello World"

    def test_textentry(self):
        layout = "{ textentry 50 50 200 20 0 0 }"
        lines = ["initial"]
        gump = parse_layout(layout, lines)
        assert len(gump.text_entries) == 1
        te = gump.text_entries[0]
        assert te.entry_id == 0
        assert te.initial_text == "initial"

    def test_checkbox(self):
        layout = "{ checkbox 10 20 210 211 1 100 }"
        gump = parse_layout(layout, [])
        assert len(gump.switches) == 1
        sw = gump.switches[0]
        assert sw.switch_id == 100
        assert sw.initial_state is True
        assert sw.is_radio is False

    def test_radio(self):
        layout = "{ radio 10 20 210 211 0 200 }"
        gump = parse_layout(layout, [])
        sw = gump.switches[0]
        assert sw.switch_id == 200
        assert sw.initial_state is False
        assert sw.is_radio is True

    def test_flags(self):
        layout = "{ noclose }{ nodispose }{ nomove }{ noresize }"
        gump = parse_layout(layout, [])
        assert gump.no_close is True
        assert gump.no_dispose is True
        assert gump.no_move is True
        assert gump.no_resize is True

    def test_multiple_commands(self):
        layout = (
            "{ resizepic 0 0 3600 400 300 }"
            "{ text 20 20 0 0 }"
            "{ text 20 50 0 1 }"
            "{ button 350 20 4005 4007 1 0 1 }"
            "{ button 350 50 4005 4007 1 0 2 }"
        )
        lines = ["Boards", "Furniture"]
        gump = parse_layout(layout, lines)
        assert len(gump.texts) == 2
        assert len(gump.buttons) == 2
        assert len(gump.reply_buttons()) == 2

    def test_buttontileart(self):
        layout = "{ buttontileart 10 20 4005 4007 1 0 5 1234 0 44 44 }"
        gump = parse_layout(layout, [])
        assert len(gump.buttons) == 1
        assert gump.buttons[0].button_id == 5

    def test_htmlgump(self):
        layout = "{ htmlgump 10 20 300 100 0 1 0 }"
        lines = ["<b>Hello</b>"]
        gump = parse_layout(layout, lines)
        assert len(gump.texts) == 1
        assert gump.texts[0].text_id == 0

    def test_croppedtext(self):
        layout = "{ croppedtext 10 20 100 30 0 }"
        gump = parse_layout(layout, ["Label"])
        assert len(gump.texts) == 1


class TestGumpDataMethods:
    def test_find_button_near_text(self):
        layout = (
            "{ text 20 20 0 0 }"
            "{ text 20 60 0 1 }"
            "{ button 350 20 4005 4007 1 0 1 }"
            "{ button 350 60 4005 4007 1 0 2 }"
        )
        lines = ["Boards", "Furniture"]
        gump = parse_layout(layout, lines)
        btn = gump.find_button_near_text("Boards")
        assert btn is not None
        assert btn.button_id == 1

        btn2 = gump.find_button_near_text("Furniture")
        assert btn2 is not None
        assert btn2.button_id == 2

    def test_find_button_near_text_case_insensitive(self):
        layout = "{ text 20 20 0 0 }{ button 350 20 4005 4007 1 0 1 }"
        gump = parse_layout(layout, ["BOARDS"])
        btn = gump.find_button_near_text("boards")
        assert btn is not None
        assert btn.button_id == 1

    def test_find_button_near_text_not_found(self):
        layout = "{ button 350 20 4005 4007 1 0 1 }"
        gump = parse_layout(layout, [])
        assert gump.find_button_near_text("nonexistent") is None

    def test_find_button_by_id(self):
        layout = "{ button 10 20 4005 4007 1 0 1 }{ button 10 40 4005 4007 1 0 2 }"
        gump = parse_layout(layout, [])
        assert gump.find_button_by_id(2) is not None
        assert gump.find_button_by_id(2).button_id == 2
        assert gump.find_button_by_id(999) is None

    def test_get_text_out_of_range(self):
        gump = parse_layout("", ["only one"])
        assert gump.get_text(0) == "only one"
        assert gump.get_text(99) == ""
        assert gump.get_text(-1) == ""


# ---------------------------------------------------------------------------
# Packet 0xB1 — GumpResponse builder
# ---------------------------------------------------------------------------


class TestBuildGumpResponse:
    def test_simple_button_press(self):
        pkt = build_gump_response(
            serial=0x00000001,
            gump_id=0x12345678,
            button_id=5,
        )
        assert pkt[0] == 0xB1
        length = struct.unpack(">H", pkt[1:3])[0]
        assert length == len(pkt)

        r = PacketReader(pkt[3:])
        assert r.read_u32() == 0x00000001  # serial
        assert r.read_u32() == 0x12345678  # gump_id
        assert r.read_u32() == 5  # button_id
        assert r.read_u32() == 0  # switch count
        assert r.read_u32() == 0  # text entry count

    def test_with_switches(self):
        pkt = build_gump_response(
            serial=0x00000001,
            gump_id=0xABCD,
            button_id=1,
            switches=[100, 200, 300],
        )
        r = PacketReader(pkt[3:])
        r.skip(4 + 4 + 4)  # serial + gump_id + button_id
        switch_count = r.read_u32()
        assert switch_count == 3
        assert r.read_u32() == 100
        assert r.read_u32() == 200
        assert r.read_u32() == 300
        text_count = r.read_u32()
        assert text_count == 0

    def test_with_text_entries(self):
        pkt = build_gump_response(
            serial=0x00000001,
            gump_id=0xABCD,
            button_id=1,
            text_entries=[(0, "Hello"), (3, "World")],
        )
        r = PacketReader(pkt[3:])
        r.skip(4 + 4 + 4)  # serial + gump_id + button_id
        assert r.read_u32() == 0  # switch count
        text_count = r.read_u32()
        assert text_count == 2
        # Entry 0
        assert r.read_u16() == 0  # entry_id
        assert r.read_u16() == 5  # char count
        text0 = r.read_unicode_be(5)
        assert text0 == "Hello"
        # Entry 1
        assert r.read_u16() == 3  # entry_id
        assert r.read_u16() == 5  # char count
        text1 = r.read_unicode_be(5)
        assert text1 == "World"

    def test_cancel(self):
        pkt = build_gump_response(
            serial=0x00000001,
            gump_id=0xABCD,
            button_id=0,  # close/cancel
        )
        r = PacketReader(pkt[3:])
        r.skip(4 + 4)
        assert r.read_u32() == 0  # button_id = cancel


# ---------------------------------------------------------------------------
# Packet handler 0xB0 — OpenGump
# ---------------------------------------------------------------------------


def _build_open_gump_packet(
    serial: int,
    gump_id: int,
    x: int,
    y: int,
    layout: str,
    text_lines: list[str],
) -> bytes:
    """Build a synthetic 0xB0 OpenGump packet for testing."""
    w = PacketWriter()
    w.write_u8(0xB0)
    w.write_u16(0)  # length placeholder
    w.write_u32(serial)
    w.write_u32(gump_id)
    w.write_u32(x)
    w.write_u32(y)
    layout_bytes = layout.encode("ascii")
    w.write_u16(len(layout_bytes))
    w.write_bytes(layout_bytes)
    w.write_u16(len(text_lines))
    for line in text_lines:
        encoded = line.encode("utf-16-be")
        w.write_u16(len(line))  # char count
        w.write_bytes(encoded)

    data = bytearray(w.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_handle_open_gump():
    h, p, _ = _make_stack()
    layout = "{ button 10 20 4005 4007 1 0 1 }{ text 50 20 0 0 }"
    lines = ["Craft Item"]
    pkt = _build_open_gump_packet(0x00001234, 0xAABBCCDD, 100, 200, layout, lines)

    h.dispatch(0xB0, pkt)

    assert 0xAABBCCDD in p.self_state.gumps
    gump = p.self_state.gumps[0xAABBCCDD]
    assert gump.serial == 0x00001234
    assert gump.gump_id == 0xAABBCCDD
    assert gump.x == 100
    assert gump.y == 200
    assert len(gump.buttons) == 1
    assert len(gump.texts) == 1
    assert gump.text_lines == ["Craft Item"]

    # Check event was emitted
    events = p.poll_events()
    gump_events = [e for e in events if e.type == GameEventType.GUMP_OPENED]
    assert len(gump_events) == 1
    assert gump_events[0].data["gump_id"] == 0xAABBCCDD


def test_handle_open_gump_replaces_existing():
    """Receiving a gump with the same ID should replace the previous one."""
    h, p, _ = _make_stack()
    pkt1 = _build_open_gump_packet(0x01, 0xFF, 0, 0, "{ button 10 20 4005 4007 1 0 1 }", ["A"])
    pkt2 = _build_open_gump_packet(0x01, 0xFF, 0, 0, "{ button 10 20 4005 4007 1 0 2 }", ["B"])

    h.dispatch(0xB0, pkt1)
    h.dispatch(0xB0, pkt2)

    gump = p.self_state.gumps[0xFF]
    assert gump.buttons[0].button_id == 2
    assert gump.text_lines == ["B"]


# ---------------------------------------------------------------------------
# Packet handler 0xDD — CompressedGump
# ---------------------------------------------------------------------------


def _build_compressed_gump_packet(
    serial: int,
    gump_id: int,
    x: int,
    y: int,
    layout: str,
    text_lines: list[str],
) -> bytes:
    """Build a synthetic 0xDD CompressedGump packet for testing."""
    w = PacketWriter()
    w.write_u8(0xDD)
    w.write_u16(0)  # length placeholder
    w.write_u32(serial)
    w.write_u32(gump_id)
    w.write_u32(x)
    w.write_u32(y)

    # Layout: compress
    layout_raw = layout.encode("ascii")
    layout_compressed = zlib.compress(layout_raw)
    w.write_u32(len(layout_compressed) + 4)  # compressed len + 4 for decompressed len
    w.write_u32(len(layout_raw))  # decompressed len
    w.write_bytes(layout_compressed)

    # Text lines: build raw then compress
    text_buf = bytearray()
    for line in text_lines:
        encoded = line.encode("utf-16-be")
        text_buf.extend(struct.pack(">H", len(line)))
        text_buf.extend(encoded)

    text_compressed = zlib.compress(bytes(text_buf))
    w.write_u32(len(text_lines))  # line count
    w.write_u32(len(text_compressed) + 4)  # compressed len + 4
    w.write_u32(len(text_buf))  # decompressed len
    w.write_bytes(text_compressed)

    data = bytearray(w.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_handle_compressed_gump():
    h, p, _ = _make_stack()
    layout = "{ button 10 20 4005 4007 1 0 5 }{ text 50 20 0 0 }{ noclose }"
    lines = ["Tinkering"]
    pkt = _build_compressed_gump_packet(0x00005678, 0x11223344, 50, 60, layout, lines)

    h.dispatch(0xDD, pkt)

    assert 0x11223344 in p.self_state.gumps
    gump = p.self_state.gumps[0x11223344]
    assert gump.serial == 0x00005678
    assert gump.gump_id == 0x11223344
    assert len(gump.buttons) == 1
    assert gump.buttons[0].button_id == 5
    assert gump.text_lines == ["Tinkering"]
    assert gump.no_close is True


def test_handle_compressed_gump_empty_text():
    h, p, _ = _make_stack()
    layout = "{ button 10 20 4005 4007 1 0 1 }"
    pkt = _build_compressed_gump_packet(0x01, 0x02, 0, 0, layout, [])

    h.dispatch(0xDD, pkt)

    gump = p.self_state.gumps[0x02]
    assert gump.text_lines == []
    assert len(gump.buttons) == 1


# ---------------------------------------------------------------------------
# Gump close via 0xBF sub 0x04
# ---------------------------------------------------------------------------


def test_close_gump_via_general_info():
    h, p, _ = _make_stack()
    # First open a gump
    pkt = _build_open_gump_packet(0x01, 0xABCD, 0, 0, "{ button 10 20 4005 4007 1 0 1 }", [])
    h.dispatch(0xB0, pkt)
    assert 0xABCD in p.self_state.gumps
    p.poll_events()  # drain

    # Now close via 0xBF sub 0x04
    w = PacketWriter()
    w.write_u8(0xBF)
    w.write_u16(0)  # length placeholder
    w.write_u16(0x04)  # subcmd = CloseGump
    w.write_u32(0xABCD)  # gump_id
    w.write_u32(0)  # button_id
    data = bytearray(w.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0xBF, bytes(data))

    assert 0xABCD not in p.self_state.gumps
    events = p.poll_events()
    close_events = [e for e in events if e.type == GameEventType.GUMP_CLOSED]
    assert len(close_events) == 1
    assert close_events[0].data["gump_id"] == 0xABCD
