"""Tests for perception packet handlers — feed crafted bytes, verify state."""

import struct

from anima.client.codec import PacketWriter
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.event_stream import GameEventType
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    """Create a wired perception + handler stack for testing."""
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_context_menu(mode: int, serial: int, entries: list[tuple]) -> bytes:
    """Build a 0xBF subcmd 0x14 DisplayContextMenu packet.

    Each `entries` tuple is (cliloc, index, flags). For the new layout
    (mode >= 2) cliloc is a full u32; for the old layout (mode < 2) it is
    encoded as a u16 with the +3_000_000 base offset stripped back off.
    """
    body = bytearray()
    body += struct.pack(">H", mode)
    body += struct.pack(">I", serial)
    body.append(len(entries))
    for cliloc, index, flags in entries:
        if mode >= 2:
            body += struct.pack(">IHH", cliloc, index, flags)
        else:
            body += struct.pack(">HHH", index, cliloc - 3_000_000, flags)
    pkt = bytearray()
    pkt.append(0xBF)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += struct.pack(">H", 0x0014)  # subcmd
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_context_menu_new_cliloc():
    h, p, _ = _make_stack()
    entries = [(1114778, 0, 0x20), (3000168, 1, 0x00)]
    h.dispatch(0xBF, _build_context_menu(mode=2, serial=0x40001234, entries=entries))
    assert p.self_state.context_menu_serial == 0x40001234
    got = [(e.cliloc, e.index, e.flags) for e in p.self_state.context_menu]
    assert got == entries


def test_context_menu_old_cliloc_layout():
    """mode < 2 uses index,u16-cliloc,flags ordering — not the new u32 cliloc.

    Regression: the handler used to decode every entry as the new layout,
    so an old-format packet produced a bogus 4-byte cliloc and a shifted
    index/flags. Verify the correct old-format fields are recovered.
    """
    h, p, _ = _make_stack()
    entries = [(3006149, 0, 0x00), (3000168, 1, 0x00)]
    h.dispatch(0xBF, _build_context_menu(mode=0, serial=0x4000ABCD, entries=entries))
    assert p.self_state.context_menu_serial == 0x4000ABCD
    got = [(e.cliloc, e.index, e.flags) for e in p.self_state.context_menu]
    assert got == entries


def test_context_menu_old_cliloc_flag_gated_words_keep_alignment():
    """Old-format entries with flag-gated trailing words must not desync.

    flags & 0x20 appends one extra u16; if the decoder ignores it the
    following entry is read at the wrong offset. Verify both entries decode.
    """
    h, p, _ = _make_stack()
    # First entry carries the 0x20 flag (one trailing word). Hand-build the
    # body so we can inject the extra word between the two entries.
    body = bytearray()
    body += struct.pack(">H", 0)            # mode 0 (old)
    body += struct.pack(">I", 0x40005555)   # serial
    body.append(2)                           # count
    body += struct.pack(">HHH", 0, 3006149 - 3_000_000, 0x20)  # entry 0
    body += struct.pack(">H", 0x1234)        # flag-gated trailing word
    body += struct.pack(">HHH", 7, 3000168 - 3_000_000, 0x00)  # entry 1
    pkt = bytearray()
    pkt.append(0xBF)
    pkt += struct.pack(">H", 0)
    pkt += struct.pack(">H", 0x0014)
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))

    h.dispatch(0xBF, bytes(pkt))
    got = [(e.cliloc, e.index, e.flags) for e in p.self_state.context_menu]
    assert got == [(3006149, 0, 0x20), (3000168, 7, 0x00)]


# ---------------------------------------------------------------------------
# Ground-item multi flag (0x1A WorldItem / 0xF3 UpdateItemSA)
# ---------------------------------------------------------------------------


def _build_world_item_sa(serial: int, data_type: int, item_id: int) -> bytes:
    """Build a 0xF3 WorldItemHS (26-byte) packet for one ground item.

    Mirrors ServUO Packets.cs WorldItemHS layout:
        [0xF3][0x0001 u16][data_type u8][serial u32][itemID u16][inc u8]
        [amount u16][amount u16][x u16][y u16][z i8][light u8][hue u16]
        [flags u8][0x0000 u16]
    """
    pkt = bytearray()
    pkt.append(0xF3)
    pkt += struct.pack(">H", 0x0001)   # unknown
    pkt.append(data_type)              # 0x00 item, 0x02 multi
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", item_id)
    pkt.append(0x00)                   # graphic_inc
    pkt += struct.pack(">H", 1)        # amount
    pkt += struct.pack(">H", 1)        # amount again
    pkt += struct.pack(">H", 1000)     # x
    pkt += struct.pack(">H", 1010)     # y
    pkt += struct.pack(">b", -5)       # z
    pkt.append(0x00)                   # light
    pkt += struct.pack(">H", 0)        # hue
    pkt.append(0x00)                   # flags
    pkt += struct.pack(">H", 0x0000)   # trailing pad
    assert len(pkt) == 26
    return bytes(pkt)


def test_update_item_sa_flags_multi():
    """0xF3 data_type 0x02 must mark the item as a multi, 0x00 must not.

    Regression: the data_type byte was read and discarded, so ground multis
    (house addons/signs) were stored as ordinary items and polluted
    nearest-item lookups — the same class of bug already fixed for 0x1A.
    """
    h, p, _ = _make_stack()

    h.dispatch(0xF3, _build_world_item_sa(0x40000010, data_type=0x00, item_id=0x0EED))
    h.dispatch(0xF3, _build_world_item_sa(0x40000011, data_type=0x02, item_id=0x0064))

    plain = p.world.items[0x40000010]
    multi = p.world.items[0x40000011]
    assert plain.is_multi is False
    assert plain.graphic == 0x0EED
    assert multi.is_multi is True


def test_world_item_1a_flags_multi():
    """0x1A with the 0x4000 multi bit set must strip it and flag is_multi."""
    h, p, _ = _make_stack()

    # serial without the 0x80000000 stack flag; graphic carries the 0x4000
    # multi bit (and NOT the 0x8000 inc bit), x/y with no high flags.
    body = bytearray()
    body += struct.pack(">I", 0x40000022)        # serial
    body += struct.pack(">H", 0x4000 | 0x003F)   # graphic = multi flag | art id
    body += struct.pack(">H", 1500)              # x
    body += struct.pack(">H", 1600)              # y
    body += struct.pack(">b", 0)                 # z
    pkt = bytearray()
    pkt.append(0x1A)
    pkt += struct.pack(">H", 0)                  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))

    h.dispatch(0x1A, bytes(pkt))
    it = p.world.items[0x40000022]
    assert it.is_multi is True
    assert it.graphic == 0x003F  # 0x4000 multi bit stripped


# ---------------------------------------------------------------------------
# MobileIncoming (0x78)
# ---------------------------------------------------------------------------


def test_mobile_incoming():
    h, p, w = _make_stack()

    # Build a 0x78 packet: variable length
    buf = PacketWriter()
    buf.write_u8(0x78)
    buf.write_u16(0)  # length placeholder
    serial = 0x00000099
    buf.write_u32(serial)
    buf.write_u16(0x0190)  # body (human male)
    buf.write_u16(1000)    # x
    buf.write_u16(2000)    # y
    buf.write_i8(10)       # z
    buf.write_u8(2)        # direction = EAST
    buf.write_u16(0x0421)  # hue
    buf.write_u8(0x00)     # flags
    buf.write_u8(1)        # notoriety = innocent
    buf.write_u32(0)       # terminator (no equipment)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0x78, bytes(data))

    mob = p.world.mobiles.get(serial)
    assert mob is not None
    assert mob.x == 1000
    assert mob.y == 2000
    assert mob.z == 10
    assert mob.body == 0x0190

    events = p.poll_events()
    assert any(e.type == GameEventType.MOBILE_APPEARED for e in events)


# ---------------------------------------------------------------------------
# MobileMoving (0x77)
# ---------------------------------------------------------------------------


def test_mobile_moving():
    h, p, w = _make_stack()

    # Pre-create a mobile
    mob = p.world.get_or_create_mobile(0x00000099)
    mob.x, mob.y = 100, 100

    # Build 0x77 (fixed 17 bytes)
    buf = PacketWriter()
    buf.write_u8(0x77)
    buf.write_u32(0x00000099)
    buf.write_u16(0x0190)   # body
    buf.write_u16(200)      # x
    buf.write_u16(300)      # y
    buf.write_i8(5)         # z
    buf.write_u8(4)         # direction = SOUTH
    buf.write_u16(0)        # hue
    buf.write_u8(0)         # flags
    buf.write_u8(1)         # notoriety

    h.dispatch(0x77, buf.to_bytes())

    assert mob.x == 200
    assert mob.y == 300

    events = p.poll_events()
    assert any(e.type == GameEventType.MOBILE_MOVED for e in events)


def test_mobile_moving_self_updates_flags():
    """0x77 for our own serial must refresh self_state.flags.

    ServUO pushes 0x77 MobileMoving to a mobile's own client on flag deltas
    (war mode, frozen, hidden, flying) — not just position. The handler used
    to early-return for self and drop the flags byte, leaving in_war_mode /
    hidden stale. Verify both directions of a war-mode toggle land.
    """
    from anima.perception.enums import MobileFlags

    h, p, w = _make_stack()
    self_serial = p.self_state.serial

    def _moving(flags: int) -> bytes:
        buf = PacketWriter()
        buf.write_u8(0x77)
        buf.write_u32(self_serial)
        buf.write_u16(0x0190)   # body
        buf.write_u16(123)      # x
        buf.write_u16(456)      # y
        buf.write_i8(0)         # z
        buf.write_u8(2)         # direction = EAST
        buf.write_u16(0)        # hue
        buf.write_u8(flags)     # flags
        buf.write_u8(1)         # notoriety
        return buf.to_bytes()

    # Enter war mode (0x40).
    h.dispatch(0x77, _moving(MobileFlags.WAR_MODE))
    assert p.self_state.in_war_mode is True
    assert p.self_state.body == 0x0190

    # Leave war mode again — stale flag must clear.
    h.dispatch(0x77, _moving(MobileFlags.NONE))
    assert p.self_state.in_war_mode is False
    assert p.self_state.hidden is False


# ---------------------------------------------------------------------------
# MobileUpdate (0x20) — self
# ---------------------------------------------------------------------------


def test_mobile_update_self():
    h, p, w = _make_stack()

    # Build 0x20 (fixed 19 bytes)
    buf = PacketWriter()
    buf.write_u8(0x20)
    buf.write_u32(0x00000001)  # player serial
    buf.write_u16(0x0190)      # body
    buf.write_u8(0)            # graphic_inc
    buf.write_u16(0)           # hue
    buf.write_u8(0)            # flags
    buf.write_u16(500)         # x
    buf.write_u16(600)         # y
    buf.write_u16(0)           # server_id
    buf.write_u8(3)            # direction
    buf.write_i8(-5)           # z

    h.dispatch(0x20, buf.to_bytes())

    assert p.self_state.x == 500
    assert p.self_state.y == 600
    assert p.self_state.z == -5
    assert p.self_state.direction == 3


# ---------------------------------------------------------------------------
# Delete (0x1D)
# ---------------------------------------------------------------------------


def test_delete_mobile():
    h, p, w = _make_stack()
    p.world.get_or_create_mobile(0x00000099)

    buf = PacketWriter()
    buf.write_u8(0x1D)
    buf.write_u32(0x00000099)

    h.dispatch(0x1D, buf.to_bytes())
    assert 0x00000099 not in p.world.mobiles

    events = p.poll_events()
    assert any(e.type == GameEventType.MOBILE_REMOVED for e in events)


def test_mobile_name_survives_delete_and_reincoming():
    # Regression: agent walked between Minoc waypoints for 1h+ because
    # provisioner NPCs re-entered view after a 0x1D Delete with blank
    # names, making _find_vendor return None every tick. OPL-cached names
    # must survive the delete/re-incoming cycle.
    h, p, w = _make_stack()
    serial = 0x000008C2

    # Seed OPL cache as if handle_mega_cliloc had already run.
    p.world.opl_names[serial] = "Veda the provisioner"
    p.world.opl_properties[serial] = [
        "Veda the provisioner",
        "Minoc Provisioner",
    ]

    # Mobile was in view, then leaves.
    p.world.get_or_create_mobile(serial)
    del_buf = PacketWriter()
    del_buf.write_u8(0x1D)
    del_buf.write_u32(serial)
    h.dispatch(0x1D, del_buf.to_bytes())
    assert serial not in p.world.mobiles

    # Mobile re-enters via 0x78 — blank payload for name on this packet.
    in_buf = PacketWriter()
    in_buf.write_u8(0x78)
    in_buf.write_u16(0)
    in_buf.write_u32(serial)
    in_buf.write_u16(0x0190)
    in_buf.write_u16(2524)
    in_buf.write_u16(547)
    in_buf.write_i8(0)
    in_buf.write_u8(0)
    in_buf.write_u16(0)
    in_buf.write_u8(0)
    in_buf.write_u8(1)
    in_buf.write_u32(0)
    data = bytearray(in_buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    h.dispatch(0x78, bytes(data))

    mob = p.world.mobiles[serial]
    assert mob.name == "Veda the provisioner"
    assert "Minoc Provisioner" in mob.properties


def test_mega_cliloc_populates_opl_cache():
    # handle_mega_cliloc must write to the OPL cache so a subsequent
    # 0x1D → 0x78 cycle restores the name.
    h, p, w = _make_stack()
    serial = 0x0000ABCD

    # Pre-create mobile and manually inject properties by calling the
    # handler with a minimal-but-valid 0xD6 payload is awkward; instead,
    # use an already-real cliloc (1050045 "~1_NAME~" with arg) — but to
    # keep the test hermetic we set properties via the mobile directly
    # and then verify the cache write path by calling the handler with
    # a handcrafted valid packet.
    #
    # Shortcut: exercise the cache write by calling handle_mega_cliloc
    # with a packet whose first cliloc is plain args (no base_text).
    buf = PacketWriter()
    buf.write_u8(0xD6)
    buf.write_u16(0)  # length placeholder
    buf.write_u16(0x0001)  # unknown
    buf.write_u32(serial)
    buf.write_u16(0x0000)  # unknown
    buf.write_u32(0x12345678)  # list_id / hash
    # Single entry: cliloc_num=1 (likely unknown → falls back to args),
    # args = "Test Vendor"
    buf.write_u32(1)
    args = "Test Vendor"
    raw_args = args.encode("utf-16-le")
    buf.write_u16(len(raw_args))
    for b in raw_args:
        buf.write_u8(b)
    buf.write_u32(0)  # terminator
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    h.dispatch(0xD6, bytes(data))

    assert p.world.opl_names.get(serial) == "Test Vendor"
    assert p.world.opl_properties.get(serial) == ["Test Vendor"]


def _build_mega_cliloc(serial: int, entries: list[tuple[int, str]]) -> bytes:
    """Build a 0xD6 MegaCliloc packet. Each entry is (cliloc_num, args)
    where args is the tab-joined unicode argument string (LE)."""
    buf = PacketWriter()
    buf.write_u8(0xD6)
    buf.write_u16(0)        # length placeholder
    buf.write_u16(0x0001)   # unknown
    buf.write_u32(serial)
    buf.write_u16(0x0000)   # unknown
    buf.write_u32(0xDEADBEEF)  # list_id / hash
    for cliloc_num, args in entries:
        buf.write_u32(cliloc_num)
        raw = args.encode("utf-16-le")
        buf.write_u16(len(raw))
        for b in raw:
            buf.write_u8(b)
    buf.write_u32(0)        # terminator
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_mega_cliloc_arg_substitution_is_index_driven(monkeypatch):
    """0xD6 OPL arg substitution must follow ClassicUO: replace ~N~ /
    ~N_label~ with the (N-1)th tab arg, regardless of placeholder order,
    repetition, or a missing underscore. The old positional loop dropped
    underscore-less placeholders (e.g. cliloc 1041522 "~1~~2~~3~") and
    only substituted the first occurrence of a repeated index."""
    import anima.perception.handlers as handlers_mod

    fake = {
        # composite, underscore-less — the old regex (~N_[^~]*~) never matched
        1041522: "~1~~2~~3~",
        # repeated placeholder index — old loop used count=1 per arg
        2000001: "~1_x~ and ~1_x~",
    }
    monkeypatch.setattr(handlers_mod, "cliloc_text", lambda n: fake.get(n, ""))

    h, p, _ = _make_stack()
    serial = 0x0000B00C
    p.world.get_or_create_mobile(serial)

    h.dispatch(
        0xD6,
        _build_mega_cliloc(
            serial,
            [
                (1041522, "Strength\t Bonus: \t5"),
                (2000001, "gold"),
            ],
        ),
    )

    props = p.world.opl_properties.get(serial)
    assert props == ["Strength Bonus: 5", "gold and gold"]
    # No raw placeholder must survive.
    assert all("~" not in line for line in props)


def test_mega_cliloc_refreshes_changed_mobile_name(monkeypatch):
    """0xD6 MegaCliloc must reflect the LATEST OPL name on a mobile, not pin
    the first one ever seen. MegaCliloc is anima's only mobile-name source, so
    a stale pin froze the name on a rename OR — worse — when the server recycles
    a freed mobile serial for a different mobile (the live mob is re-seeded from
    the opl_names cache, and the pin then blocked the new mobile's correct OPL
    name). Matches ClassicUO MegaCliloc (entity reflects the latest OPL string)
    and keeps mob.name consistent with the opl_names cache."""
    import anima.perception.handlers as handlers_mod

    # No base cliloc text → the args string becomes the property/name verbatim.
    monkeypatch.setattr(handlers_mod, "cliloc_text", lambda n: "")

    h, p, _ = _make_stack()
    serial = 0x0000C0DE
    p.world.get_or_create_mobile(serial)

    # First OPL: mobile is "Adranath".
    h.dispatch(0xD6, _build_mega_cliloc(serial, [(1, "Adranath")]))
    assert p.world.mobiles[serial].name == "Adranath"
    assert p.world.opl_names[serial] == "Adranath"

    # A later OPL for the SAME serial carries a different name (rename, or a
    # recycled serial now belonging to another mobile). The live mobile must
    # follow it — not keep "Adranath" — and stay in sync with the cache.
    h.dispatch(0xD6, _build_mega_cliloc(serial, [(1, "Veda the provisioner")]))
    assert p.world.mobiles[serial].name == "Veda the provisioner"
    assert p.world.opl_names[serial] == "Veda the provisioner"

    # Same divergence for items (ClassicUO always sets entity.Name on items).
    item_serial = 0x40000123
    p.world.get_or_create_item(item_serial)
    h.dispatch(0xD6, _build_mega_cliloc(item_serial, [(1, "a dull sword")]))
    assert p.world.items[item_serial].name == "a dull sword"
    h.dispatch(0xD6, _build_mega_cliloc(item_serial, [(1, "a sharp katana")]))
    assert p.world.items[item_serial].name == "a sharp katana"


# ---------------------------------------------------------------------------
# Cliloc Affix (0xCC SendLocalizedMessageAffix)
# ---------------------------------------------------------------------------


def _build_cliloc_affix(
    serial: int,
    cliloc_num: int,
    flags: int,
    name: str,
    affix: str,
    args: str,
    *,
    msg_type: int = 0,
    hue: int = 0x03B2,
) -> bytes:
    """Build a 0xCC SendLocalizedMessageAffix packet.

    Layout (per ClassicUO DisplayClilocString, 0xCC branch): the args are
    UTF-16 *Big-Endian*, there is an extra 1-byte flags field after the
    cliloc id, and a NUL-terminated ASCII affix follows the 30-byte name.
    """
    buf = PacketWriter()
    buf.write_u8(0xCC)
    buf.write_u16(0)            # length placeholder
    buf.write_u32(serial)
    buf.write_u16(0x0190)       # graphic
    buf.write_u8(msg_type)
    buf.write_u16(hue)
    buf.write_u16(3)            # font
    buf.write_u32(cliloc_num)
    buf.write_u8(flags)         # AffixType
    buf.write_ascii(name, 30)
    buf.write_bytes(affix.encode("ascii") + b"\x00")  # NUL-terminated affix
    buf.write_bytes(args.encode("utf-16-be"))         # BE args, no terminator
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_cliloc_affix_decodes_be_args_and_appends_affix(monkeypatch):
    """0xCC must be routed at all (previously unhandled → dropped), and it
    must decode using the 0xCC-specific layout: a flags byte, a variable
    NUL-terminated affix, and UTF-16 *Big-Endian* args. Appending (flags=0)
    puts the affix after the resolved cliloc text."""
    import anima.perception.handlers as handlers_mod

    # cliloc 1042971 is the classic "~1_NOTHING~" passthrough used for free
    # text; use a placeholder that exercises arg substitution.
    monkeypatch.setattr(
        handlers_mod, "cliloc_text", lambda n: "You see: ~1_val~" if n == 500000 else ""
    )

    h, p, _ = _make_stack()

    pkt = _build_cliloc_affix(
        serial=0x00000099,
        cliloc_num=500000,
        flags=0x00,            # Append
        name="Guard",
        affix=" [criminal]",
        args="a sword",
    )
    h.dispatch(0xCC, pkt)

    # The packet was routed (not silently dropped).
    assert len(p.social.journal) == 1
    entry = p.social.journal[0]
    assert entry.name == "Guard"
    # BE args decoded correctly + affix appended after the text.
    assert entry.text == "You see: a sword [criminal]"
    assert "\x00" not in entry.text

    events = p.poll_events()
    assert any(e.type == GameEventType.SPEECH_HEARD for e in events)


def test_cliloc_affix_prepend_flag(monkeypatch):
    """flags bit 0x01 (Prepend) puts the affix before the cliloc text."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(
        handlers_mod, "cliloc_text", lambda n: "the dragon" if n == 500001 else ""
    )

    h, p, _ = _make_stack()
    pkt = _build_cliloc_affix(
        serial=0x000000AA,
        cliloc_num=500001,
        flags=0x01,            # Prepend
        name="",
        affix="An angry ",
        args="",
    )
    h.dispatch(0xCC, pkt)

    assert len(p.social.journal) == 1
    assert p.social.journal[0].text == "An angry the dragon"
    # Empty name falls back to "System".
    assert p.social.journal[0].name == "System"


# ---------------------------------------------------------------------------
# HP Update (0xA1)
# ---------------------------------------------------------------------------


def test_hp_update_self():
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0xA1)
    buf.write_u32(0x00000001)  # player serial
    buf.write_u16(100)         # hits_max
    buf.write_u16(75)          # hits

    h.dispatch(0xA1, buf.to_bytes())

    assert p.self_state.hits == 75
    assert p.self_state.hits_max == 100
    assert p.self_state.hp_percent == 75.0

    events = p.poll_events()
    assert any(e.type == GameEventType.HP_CHANGED for e in events)


def test_hp_update_other():
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0xA1)
    buf.write_u32(0x00000099)  # other serial
    buf.write_u16(100)         # hits_max
    buf.write_u16(50)          # hits

    h.dispatch(0xA1, buf.to_bytes())

    mob = p.world.mobiles[0x00000099]
    assert mob.hits == 50
    assert mob.hits_max == 100


# ---------------------------------------------------------------------------
# MobileAttributes (0x2D) — full vitals refresh in one packet
# ---------------------------------------------------------------------------


def test_mobile_attributes_self_updates_all_vitals():
    h, p, w = _make_stack()
    # Seed stale vitals so we can prove every field is overwritten.
    p.self_state.hits = 1
    p.self_state.hits_max = 1
    p.self_state.mana = 1
    p.self_state.mana_max = 1
    p.self_state.stam = 1
    p.self_state.stam_max = 1

    buf = PacketWriter()
    buf.write_u8(0x2D)
    buf.write_u32(0x00000001)  # player serial
    buf.write_u16(100)         # hits_max
    buf.write_u16(80)          # hits
    buf.write_u16(60)          # mana_max
    buf.write_u16(45)          # mana
    buf.write_u16(70)          # stam_max
    buf.write_u16(35)          # stam

    assert h.dispatch(0x2D, buf.to_bytes()) is True

    assert p.self_state.hits == 80
    assert p.self_state.hits_max == 100
    assert p.self_state.mana == 45
    assert p.self_state.mana_max == 60
    assert p.self_state.stam == 35
    assert p.self_state.stam_max == 70

    events = p.poll_events()
    types = {e.type for e in events}
    assert GameEventType.HP_CHANGED in types
    assert GameEventType.MANA_CHANGED in types
    assert GameEventType.STAM_CHANGED in types


def test_mobile_attributes_other_updates_hits_only():
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0x2D)
    buf.write_u32(0x00000099)  # other serial
    buf.write_u16(200)         # hits_max
    buf.write_u16(120)         # hits
    buf.write_u16(50)          # mana_max
    buf.write_u16(50)          # mana
    buf.write_u16(50)          # stam_max
    buf.write_u16(50)          # stam

    h.dispatch(0x2D, buf.to_bytes())

    mob = p.world.mobiles[0x00000099]
    assert mob.hits == 120
    assert mob.hits_max == 200


# ---------------------------------------------------------------------------
# ConfirmWalk (0x22) and DenyWalk (0x21)
# ---------------------------------------------------------------------------


def test_confirm_walk():
    h, p, w = _make_stack()
    w.steps_count = 3

    buf = PacketWriter()
    buf.write_u8(0x22)
    buf.write_u8(5)     # seq
    buf.write_u8(0)     # notoriety (unused here)

    h.dispatch(0x22, buf.to_bytes())

    assert w.steps_count == 2

    events = p.poll_events()
    assert any(e.type == GameEventType.WALK_CONFIRMED for e in events)


def test_deny_walk():
    h, p, w = _make_stack()
    w.steps_count = 3

    buf = PacketWriter()
    buf.write_u8(0x21)
    buf.write_u8(5)     # seq
    buf.write_u16(300)  # x
    buf.write_u16(400)  # y
    buf.write_u8(2)     # direction
    buf.write_i8(10)    # z

    h.dispatch(0x21, buf.to_bytes())

    assert w.steps_count == 0
    assert p.self_state.x == 300
    assert p.self_state.y == 400
    assert p.self_state.z == 10

    events = p.poll_events()
    assert any(e.type == GameEventType.WALK_DENIED for e in events)


# ---------------------------------------------------------------------------
# ASCII Talk (0x1C) and Unicode Talk (0xAE)
# ---------------------------------------------------------------------------


def test_ascii_talk():
    h, p, w = _make_stack()

    # Build variable-length 0x1C
    buf = PacketWriter()
    buf.write_u8(0x1C)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(0x00000099)  # serial
    buf.write_u16(0x0190)      # graphic
    buf.write_u8(0)            # msg_type = REGULAR
    buf.write_u16(0x0034)      # hue
    buf.write_u16(3)           # font
    buf.write_ascii("Alice", 30)
    text = b"Hello there!\x00"
    buf.write_bytes(text)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0x1C, bytes(data))

    assert len(p.social.journal) == 1
    assert p.social.journal[0].name == "Alice"
    assert p.social.journal[0].text == "Hello there!"

    events = p.poll_events()
    assert any(e.type == GameEventType.SPEECH_HEARD for e in events)


def test_unicode_talk():
    h, p, w = _make_stack()

    # Build variable-length 0xAE
    buf = PacketWriter()
    buf.write_u8(0xAE)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(0x00000099)  # serial
    buf.write_u16(0x0190)      # graphic
    buf.write_u8(0)            # msg_type
    buf.write_u16(0x0034)      # hue
    buf.write_u16(3)           # font
    buf.write_ascii("ENU", 4)  # lang
    buf.write_ascii("Bob", 30)
    text_unicode = "Hi!".encode("utf-16-be") + b"\x00\x00"
    buf.write_bytes(text_unicode)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0xAE, bytes(data))

    assert len(p.social.journal) == 1
    assert p.social.journal[0].name == "Bob"
    assert p.social.journal[0].text == "Hi!"


# ---------------------------------------------------------------------------
# GeneralInfo (0xBF) — fastwalk keys
# ---------------------------------------------------------------------------


def test_fastwalk_keys_set():
    h, p, w = _make_stack()

    # Build 0xBF subcmd 0x01 (set keys)
    buf = PacketWriter()
    buf.write_u8(0xBF)
    buf.write_u16(0)  # length placeholder
    buf.write_u16(0x01)  # subcmd
    for i in range(6):
        buf.write_u32(0x11111111 * (i + 1))

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0xBF, bytes(data))

    assert w.fast_walk_keys[0] == 0x11111111
    assert w.fast_walk_keys[4] == 0x55555555


def test_fastwalk_key_add():
    h, p, w = _make_stack()

    # Build 0xBF subcmd 0x02 (add key)
    buf = PacketWriter()
    buf.write_u8(0xBF)
    buf.write_u16(0)  # length placeholder
    buf.write_u16(0x02)  # subcmd
    buf.write_u32(0xAABBCCDD)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0xBF, bytes(data))

    assert 0xAABBCCDD in w.fast_walk_keys


# ---------------------------------------------------------------------------
# Equipment (0x2E) — player equipment
# ---------------------------------------------------------------------------


def test_equipment_self():
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0x2E)
    buf.write_u32(0x40001234)  # item serial
    buf.write_u16(0x1F03)      # graphic (sword)
    buf.write_u8(0)            # unknown
    buf.write_u8(0x01)         # layer = ONE_HANDED
    buf.write_u32(0x00000001)  # parent = player
    buf.write_u16(0)           # hue

    h.dispatch(0x2E, buf.to_bytes())

    assert p.self_state.equipment.get(0x01) == 0x40001234
    assert 0x40001234 in p.world.items


def test_equipment_graphic_increment_applied():
    # ClassicUO EquipItem computes graphic = base_u16 + signed_i8. The byte
    # after the base graphic is a signed increment, not padding. A nonzero
    # increment must shift item.graphic AND keep every following field aligned.
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0x2E)
    buf.write_u32(0x40005678)  # item serial
    buf.write_u16(0x1F00)      # base graphic
    buf.write_i8(3)            # signed graphic increment (+3)
    buf.write_u8(0x02)         # layer = TWO_HANDED
    buf.write_u32(0x00000001)  # parent = player
    buf.write_u16(0x0021)      # hue

    h.dispatch(0x2E, buf.to_bytes())

    item = p.world.items[0x40005678]
    # graphic carries the increment ...
    assert item.graphic == 0x1F03
    # ... and the trailing fields stay aligned (no desync).
    assert item.layer == 0x02
    assert item.hue == 0x0021
    assert p.self_state.equipment.get(0x02) == 0x40005678


# ---------------------------------------------------------------------------
# PacketHandler dispatch
# ---------------------------------------------------------------------------


def test_dispatch_unknown_returns_false():
    h, p, w = _make_stack()
    assert h.dispatch(0xFE, b"\xFE") is False


def test_dispatch_known_returns_true():
    h, p, w = _make_stack()
    # 0x22 is registered (ConfirmWalk)
    buf = PacketWriter()
    buf.write_u8(0x22)
    buf.write_u8(0)
    buf.write_u8(0)
    assert h.dispatch(0x22, buf.to_bytes()) is True


# ---------------------------------------------------------------------------
# SkillUpdate (0x3A)
# ---------------------------------------------------------------------------


def _build_skill_packet(list_type: int, skills: list[tuple[int, int, int, int, int]]) -> bytes:
    """Build a 0x3A skill update packet.

    skills: list of (skill_id, value, base, lock, cap) — values in tenths.
    """
    buf = PacketWriter()
    buf.write_u8(0x3A)
    buf.write_u16(0)  # length placeholder
    buf.write_u8(list_type)

    has_cap = list_type in (0x02, 0x03, 0xDF, 0xFF)

    for sid, val, base, lock, cap in skills:
        buf.write_u16(sid)
        buf.write_u16(val)
        buf.write_u16(base)
        buf.write_u8(lock)
        if has_cap:
            buf.write_u16(cap)

    # Terminator for full lists
    if list_type in (0x00, 0x01, 0x02, 0x03):
        buf.write_u16(0)  # skill_id=0 terminates

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_skill_full_list_type_0x00():
    """Full skill list (no caps), skill IDs are 1-based."""
    h, p, w = _make_stack()

    # Server sends 1-based IDs: 1=Alchemy(0), 41=Swordsmanship(40)
    packet = _build_skill_packet(0x00, [
        (1, 500, 500, 0, 0),    # Alchemy (id=0 after adjust), 50.0
        (41, 300, 300, 2, 0),   # Swordsmanship (id=40 after adjust), 30.0
    ])
    h.dispatch(0x3A, packet)

    assert 0 in p.self_state.skills  # Alchemy
    assert p.self_state.skills[0].value == 50.0
    assert p.self_state.skills[0].lock.value == 0  # UP

    assert 40 in p.self_state.skills  # Swordsmanship
    assert p.self_state.skills[40].value == 30.0
    assert p.self_state.skills[40].lock.value == 2  # LOCKED


def test_skill_full_list_with_caps_type_0x02():
    """Full skill list WITH caps, skill IDs are 1-based."""
    h, p, w = _make_stack()

    packet = _build_skill_packet(0x02, [
        (46, 750, 750, 0, 1000),  # Mining (id=45 after adjust), 75.0, cap=100.0
    ])
    h.dispatch(0x3A, packet)

    assert 45 in p.self_state.skills  # Mining
    assert p.self_state.skills[45].value == 75.0
    assert p.self_state.skills[45].cap == 100.0


def test_skill_single_update_type_0xFF():
    """Single skill update (with cap), skill ID is 0-based (no adjustment)."""
    h, p, w = _make_stack()

    packet = _build_skill_packet(0xFF, [
        (25, 800, 800, 2, 1000),  # Magery, 80.0, cap=100.0
    ])
    h.dispatch(0x3A, packet)

    assert 25 in p.self_state.skills  # Magery (no ID adjustment for 0xFF)
    assert p.self_state.skills[25].value == 80.0
    assert p.self_state.skills[25].cap == 100.0
    assert p.self_state.skills[25].lock.value == 2  # LOCKED


def test_skill_single_update_type_0xDF():
    """Single skill update (with cap), type 0xDF."""
    h, p, w = _make_stack()

    packet = _build_skill_packet(0xDF, [
        (7, 600, 600, 1, 1000),  # Blacksmith, 60.0
    ])
    h.dispatch(0x3A, packet)

    assert 7 in p.self_state.skills
    assert p.self_state.skills[7].value == 60.0
    assert p.self_state.skills[7].lock.value == 1  # DOWN


def test_skill_single_update_gain_measured_off_base_not_value():
    """A real skill gain (base advances) logs a gain message + diff event."""
    h, p, w = _make_stack()

    # Seed via a full list (never emits the gain message): Blacksmith base 60.0.
    h.dispatch(0x3A, _build_skill_packet(0x02, [
        (8, 600, 600, 0, 1000),  # Blacksmith (id=7 after the 0x02 adjust)
    ]))
    p.poll_events()  # drain

    # Single update: base climbs 60.0 -> 60.5 (a genuine training gain).
    h.dispatch(0x3A, _build_skill_packet(0xDF, [
        (7, 605, 605, 0, 1000),
    ]))
    events = p.poll_events()
    gain_events = [e for e in events
                   if e.type == GameEventType.SKILL_CHANGED and "diff" in e.data]
    assert len(gain_events) == 1
    assert gain_events[0].data["diff"] == 0.5
    assert any("Blacksmith" in entry.text for entry in p.social.recent(20))


def test_skill_value_only_change_is_not_a_phantom_gain():
    """Equipping a skill-bonus item shifts value but NOT base — no gain.

    Regression: gain detection keyed off `value` flagged gear/buff swaps as
    skill gains, polluting the activity feed and SKILL_CHANGED `diff` consumers.
    Real progression registers on `base`, so a value-only change must be silent.
    """
    h, p, w = _make_stack()

    # Baseline Magery base/value 80.0.
    h.dispatch(0x3A, _build_skill_packet(0x02, [
        (26, 800, 800, 0, 1000),  # Magery (id=25 after adjust)
    ]))
    p.poll_events()
    journal_before = len(p.social.recent(50))

    # Equip a +10 Magery item: value 90.0, base still 80.0.
    h.dispatch(0x3A, _build_skill_packet(0xDF, [
        (25, 900, 800, 0, 1000),
    ]))
    events = p.poll_events()

    # value is still tracked for callers that want the effective skill...
    assert p.self_state.skills[25].value == 90.0
    assert p.self_state.skills[25].base == 80.0
    # ...but no phantom gain message or diff event was produced.
    gain_events = [e for e in events
                   if e.type == GameEventType.SKILL_CHANGED and "diff" in e.data]
    assert gain_events == []
    assert len(p.social.recent(50)) == journal_before


def _build_skill_packet_0based(list_type: int, skills: list[tuple[int, int, int, int, int]]) -> bytes:
    """Build a 0x3A full-list packet for the 0-based variants (type 0x01/0x03).

    Per ClassicUO these carry a cap field and use raw (0-based) skill ids with
    no terminating zero entry; the list ends when the buffer is exhausted.
    skills: list of (skill_id, value, base, lock, cap) — values in tenths.
    """
    buf = PacketWriter()
    buf.write_u8(0x3A)
    buf.write_u16(0)  # length placeholder
    buf.write_u8(list_type)
    for sid, val, base, lock, cap in skills:
        buf.write_u16(sid)
        buf.write_u16(val)
        buf.write_u16(base)
        buf.write_u8(lock)
        buf.write_u16(cap)
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_skill_full_list_refresh_type_0x01_no_id_adjustment():
    """Type 0x01 full-list refresh: ids are already 0-based and carry a cap.

    Regression for an off-by-one that decremented 0x01/0x03 ids (landing every
    skill on the wrong slot) and read no cap, misaligning the rest of the list.
    """
    h, p, w = _make_stack()

    packet = _build_skill_packet_0based(0x01, [
        (25, 800, 800, 0, 1200),   # Magery — id stays 25, cap 120.0
        (45, 750, 700, 2, 1000),   # Mining — id stays 45, cap 100.0
    ])
    h.dispatch(0x3A, packet)

    # No off-by-one: ids are recorded verbatim, not shifted to 24/44.
    assert 25 in p.self_state.skills and 24 not in p.self_state.skills
    assert p.self_state.skills[25].value == 80.0
    assert p.self_state.skills[25].cap == 120.0  # cap field actually consumed

    # Stream stayed aligned, so the second entry decoded correctly too.
    assert 45 in p.self_state.skills and 44 not in p.self_state.skills
    assert p.self_state.skills[45].value == 75.0
    assert p.self_state.skills[45].base == 70.0
    assert p.self_state.skills[45].cap == 100.0
    assert p.self_state.skills[45].lock.value == 2  # LOCKED


def test_skill_cap_defaults_to_100():
    """Full list without caps (type 0x00) should default cap to 100.0."""
    h, p, w = _make_stack()

    packet = _build_skill_packet(0x00, [
        (1, 100, 100, 0, 0),  # Alchemy (id=0 after adjust)
    ])
    h.dispatch(0x3A, packet)

    assert p.self_state.skills[0].cap == 100.0  # default


def test_skill_type_0xFE_ignored():
    """Skill name list (0xFE) should be ignored without error."""
    h, p, w = _make_stack()

    buf = PacketWriter()
    buf.write_u8(0x3A)
    buf.write_u16(0)
    buf.write_u8(0xFE)
    buf.write_u16(5)  # count
    # Some garbage data
    buf.write_bytes(b"SomeSkillName\x00")

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0x3A, bytes(data))
    assert len(p.self_state.skills) == 0  # nothing parsed


# ---------------------------------------------------------------------------
# ClilocMessage (0xC1) — tilde substitution variants
# ---------------------------------------------------------------------------


def _build_cliloc_packet(
    serial: int,
    cliloc_num: int,
    name: str,
    args: str,
    msg_type: int = 6,
    hue: int = 0,
    font: int = 3,
) -> bytes:
    """Build a 0xC1 ClilocMessage packet for testing."""
    buf = PacketWriter()
    buf.write_u8(0xC1)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_u16(0)  # graphic
    buf.write_u8(msg_type)
    buf.write_u16(hue)
    buf.write_u16(font)
    buf.write_u32(cliloc_num)

    name_bytes = name.encode("ascii")[:30].ljust(30, b"\x00")
    buf.write_bytes(name_bytes)

    args_encoded = args.encode("utf-16-le") + b"\x00\x00"
    buf.write_bytes(args_encoded)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_cliloc_amount_substitution(monkeypatch):
    """~1_AMOUNT~ (bank balance) must be substituted, not just ~1_val~."""
    h, p, w = _make_stack()

    monkeypatch.setattr(
        "anima.perception.handlers.cliloc_text",
        lambda n: {1042759: "Thy current bank balance is ~1_AMOUNT~ gold."}.get(n, ""),
    )

    captured = []
    p.events.subscribe_sync(lambda e: captured.append(e))

    pkt = _build_cliloc_packet(
        serial=0x0099, cliloc_num=1042759, name="Adeline the banker", args="1234",
    )
    h.dispatch(0xC1, pkt)

    speech_events = [e for e in captured if e.type == GameEventType.SPEECH_HEARD]
    assert len(speech_events) == 1
    text = speech_events[0].data["text"]
    assert "1234" in text, f"Amount not substituted: {text!r}"
    assert "~" not in text, f"Unresolved tilde marker: {text!r}"


def test_cliloc_val_substitution(monkeypatch):
    """~1_val~ patterns still work after the regex change."""
    h, p, w = _make_stack()

    monkeypatch.setattr(
        "anima.perception.handlers.cliloc_text",
        lambda n: {500000: "You see ~1_val~ here."}.get(n, ""),
    )

    captured = []
    p.events.subscribe_sync(lambda e: captured.append(e))

    pkt = _build_cliloc_packet(
        serial=0x0099, cliloc_num=500000, name="System", args="a pile of gold",
    )
    h.dispatch(0xC1, pkt)

    speech_events = [e for e in captured if e.type == GameEventType.SPEECH_HEARD]
    assert len(speech_events) == 1
    text = speech_events[0].data["text"]
    assert "a pile of gold" in text, f"~1_val~ not substituted: {text!r}"


# ---------------------------------------------------------------------------
# ContainerContent (0x3C) — must drop stale items on refresh
# ---------------------------------------------------------------------------


def _build_container_content(items: list[tuple[int, int, int, int, int, int]]) -> bytes:
    """Build a 0x3C packet. Each item: (serial, graphic, amount, x, y, container)."""
    body = struct.pack(">H", len(items))
    for serial, graphic, amount, x, y, container in items:
        body += struct.pack(
            ">IHBHHHBIH",
            serial, graphic, 0, amount, x, y, 0, container, 0,
        )
    length = 3 + len(body)
    return struct.pack(">BH", 0x3C, length) + body


def test_container_content_clears_stale_items():
    """A second 0x3C for the same container drops items missing from the new payload.

    Without this, vendor buy-list correlation (0x74 sorts items in the
    container by (x,y)) gets polluted by old serials and the agent ends up
    sending a stale serial — the server replies "Thou hast bought nothing!".
    """
    h, p, _ = _make_stack()
    container = 0x40000001

    # Initial vendor inventory: 3 items.
    h.dispatch(0x3C, _build_container_content([
        (0x50000001, 0x0FBB, 1, 1, 1, container),  # tongs
        (0x50000002, 0x1034, 1, 2, 1, container),  # saw
        (0x50000003, 0x0E86, 1, 3, 1, container),  # pickaxe
    ]))
    assert {it.serial for it in p.world.items.values() if it.container == container} == {
        0x50000001, 0x50000002, 0x50000003,
    }

    # Vendor restocks with 2 entirely new serials.
    h.dispatch(0x3C, _build_container_content([
        (0x60000001, 0x0FBB, 1, 1, 1, container),
        (0x60000002, 0x1034, 1, 2, 1, container),
    ]))
    in_container = {it.serial for it in p.world.items.values() if it.container == container}
    assert in_container == {0x60000001, 0x60000002}, (
        f"stale items leaked into container: {in_container}"
    )


def _build_container_content_inc(
    items: list[tuple[int, int, int, int, int, int, int]],
) -> bytes:
    """Build a 0x3C packet exposing the graphic-increment byte.

    Each item: (serial, graphic, graphic_inc, amount, x, y, container).
    """
    body = struct.pack(">H", len(items))
    for serial, graphic, graphic_inc, amount, x, y, container in items:
        body += struct.pack(
            ">IHBHHHBIH",
            serial, graphic, graphic_inc, amount, x, y, 0, container, 0,
        )
    length = 3 + len(body)
    return struct.pack(">BH", 0x3C, length) + body


def test_container_content_adds_graphic_increment():
    """0x3C item graphic must be base + increment byte (ClassicUO semantics).

    ClassicUO decodes ``graphic = ReadUInt16BE() + ReadUInt8()``; the
    increment byte selects an animation/variant frame and must be folded
    into the graphic id, not dropped. Dropping it makes variant items
    (e.g. animated tiles) collide on a single base id, which breaks the
    0x74 vendor buy-list correlation that matches by graphic.
    """
    h, p, _ = _make_stack()
    container = 0x40000001

    h.dispatch(0x3C, _build_container_content_inc([
        (0x50000001, 0x0FBB, 0, 1, 1, 1, container),  # no increment
        (0x50000002, 0x1F2D, 3, 1, 2, 1, container),  # +3 variant
    ]))

    items = {it.serial: it for it in p.world.items.values()}
    assert items[0x50000001].graphic == 0x0FBB
    assert items[0x50000002].graphic == 0x1F2D + 3, (
        f"graphic increment was dropped: got 0x{items[0x50000002].graphic:04X}"
    )


def test_add_item_to_container_adds_graphic_increment():
    """0x25 AddItemToContainer must also fold the increment byte into graphic."""
    h, p, _ = _make_stack()
    container = 0x40000001
    serial = 0x50000010
    # 0x25: id, serial, graphic, graphic_inc, amount, x, y, grid_index,
    #       container, hue  (fixed length 21).
    packet = struct.pack(
        ">BIHBHHHBIH",
        0x25, serial, 0x09E2, 5, 1, 4, 4, 0, container, 0,
    )
    h.dispatch(0x25, packet)

    item = p.world.items[serial]
    assert item.graphic == 0x09E2 + 5, (
        f"graphic increment was dropped: got 0x{item.graphic:04X}"
    )
    assert item.container == container


def test_container_content_does_not_clear_other_containers():
    """Refreshing container A must not touch items that belong to container B."""
    h, p, _ = _make_stack()
    container_a = 0x40000001
    container_b = 0x40000002

    h.dispatch(0x3C, _build_container_content([
        (0x50000001, 0x0FBB, 1, 1, 1, container_a),
        (0x50000002, 0x1034, 1, 1, 1, container_b),
    ]))

    # Refresh only container A with a different serial.
    h.dispatch(0x3C, _build_container_content([
        (0x60000001, 0x0FBB, 1, 1, 1, container_a),
    ]))

    by_container = {
        it.container: it.serial for it in p.world.items.values()
    }
    assert by_container.get(container_a) == 0x60000001
    assert by_container.get(container_b) == 0x50000002


# ---------------------------------------------------------------------------
# CharacterStatus (0x11) — self full stat update
# ---------------------------------------------------------------------------


def _build_status_aos(serial: int) -> bytes:
    """Build a 0x11 type-4 (AOS) self-status packet matching ServUO's
    MobileStatus wire order: extended block, stat_cap+followers, then the
    resist/luck/damage/tithing run.
    """
    buf = PacketWriter()
    buf.write_u8(0x11)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_ascii("Tester", 30)
    buf.write_u16(48)   # hits
    buf.write_u16(50)   # hits_max
    buf.write_u8(0)     # name_change_flag
    buf.write_u8(4)     # type/flag = 4 (AOS)
    # extended block (type >= 1)
    buf.write_u8(0)     # sex
    buf.write_u16(80)   # str
    buf.write_u16(70)   # dex
    buf.write_u16(60)   # int
    buf.write_u16(45)   # stam
    buf.write_u16(50)   # stam_max
    buf.write_u16(40)   # mana
    buf.write_u16(55)   # mana_max
    buf.write_u32(1234) # gold
    buf.write_u16(20)   # phys resist / armor
    buf.write_u16(150)  # weight
    # stat_cap + followers (always present for self)
    buf.write_u16(225)  # stat_cap
    buf.write_u8(2)     # followers
    buf.write_u8(5)     # followers_max
    # AOS block (type >= 4)
    buf.write_u16(11)   # fire
    buf.write_u16(12)   # cold
    buf.write_u16(13)   # poison
    buf.write_u16(14)   # energy
    buf.write_u16(300)  # luck
    buf.write_u16(7)    # damage_min
    buf.write_u16(19)   # damage_max
    buf.write_u32(99)   # tithing points

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_character_status_aos_decodes_luck_and_damage():
    """Regression: on a stock AOS shard (type 4) luck/damage_min/damage_max
    were gated behind flag>=6 and never decoded, and stat_cap/followers
    framing must stay aligned so the resist block reads correctly.
    """
    h, p, w = _make_stack()
    serial = p.self_state.serial  # _make_stack uses 0x00000001

    h.dispatch(0x11, _build_status_aos(serial))

    ss = p.self_state
    assert ss.strength == 80
    assert ss.stat_cap == 225
    assert ss.followers == 2
    assert ss.followers_max == 5
    assert ss.resist_fire == 11
    assert ss.resist_cold == 12
    assert ss.resist_poison == 13
    assert ss.resist_energy == 14
    # These three were silently dropped before the fix.
    assert ss.luck == 300
    assert ss.damage_min == 7
    assert ss.damage_max == 19


def _build_status_aos_negative_resists(serial: int) -> bytes:
    """0x11 type-4 (AOS) self-status carrying a negative physical/armor resist
    and a negative fire resist, exactly as ServUO emits them: a signed (short)
    cast, i.e. two's-complement bytes on the wire (write_u16 masks to 0xFFFF).
    """
    buf = PacketWriter()
    buf.write_u8(0x11)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_ascii("Tester", 30)
    buf.write_u16(48)   # hits
    buf.write_u16(50)   # hits_max
    buf.write_u8(0)     # name_change_flag
    buf.write_u8(4)     # type/flag = 4 (AOS)
    # extended block (type >= 1)
    buf.write_u8(0)     # sex
    buf.write_u16(80)   # str
    buf.write_u16(70)   # dex
    buf.write_u16(60)   # int
    buf.write_u16(45)   # stam
    buf.write_u16(50)   # stam_max
    buf.write_u16(40)   # mana
    buf.write_u16(55)   # mana_max
    buf.write_u32(1234) # gold
    buf.write_u16(-3 & 0xFFFF)   # phys resist / armor (negative)
    buf.write_u16(150)  # weight
    # stat_cap + followers (always present for self)
    buf.write_u16(225)  # stat_cap
    buf.write_u8(2)     # followers
    buf.write_u8(5)     # followers_max
    # AOS block (type >= 4)
    buf.write_u16(-5 & 0xFFFF)   # fire (negative)
    buf.write_u16(12)   # cold
    buf.write_u16(13)   # poison
    buf.write_u16(14)   # energy
    buf.write_u16(300)  # luck
    buf.write_u16(7)    # damage_min
    buf.write_u16(19)   # damage_max
    buf.write_u32(99)   # tithing points

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_character_status_decodes_negative_resists_signed():
    """Regression: ServUO/ClassicUO treat the physical and AOS resists as
    signed (short). A negative resist (from a negative ResistanceMod) must
    decode to its negative value, not wrap to ~65500.
    """
    h, p, w = _make_stack()
    serial = p.self_state.serial

    h.dispatch(0x11, _build_status_aos_negative_resists(serial))

    ss = p.self_state
    # Alignment unaffected: the following block still reads correctly.
    assert ss.resist_cold == 12
    assert ss.luck == 300
    assert ss.damage_max == 19
    # The fix: negatives stay negative instead of wrapping to 65533 / 65531.
    assert ss.armor == -3
    assert ss.resist_fire == -5


def _build_status_ml(serial: int, *, female: bool, race: int) -> bytes:
    """Build a 0x11 type-5 (ML) self-status packet matching ServUO's
    MobileStatusExtended wire order: gender byte, extended stats, the
    type>=5 weight_max + race pair, stat_cap+followers, then the AOS run.
    """
    buf = PacketWriter()
    buf.write_u8(0x11)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_ascii("Tester", 30)
    buf.write_u16(48)   # hits
    buf.write_u16(50)   # hits_max
    buf.write_u8(0)     # name_change_flag (renamable)
    buf.write_u8(5)     # type/flag = 5 (ML)
    # extended block (type >= 1)
    buf.write_u8(1 if female else 0)  # gender
    buf.write_u16(80)   # str
    buf.write_u16(70)   # dex
    buf.write_u16(60)   # int
    buf.write_u16(45)   # stam
    buf.write_u16(50)   # stam_max
    buf.write_u16(40)   # mana
    buf.write_u16(55)   # mana_max
    buf.write_u32(1234) # gold
    buf.write_u16(20)   # phys resist / armor
    buf.write_u16(150)  # weight
    # type >= 5: weight_max + race (RaceID + 1)
    buf.write_u16(510)  # weight_max
    buf.write_u8(race)  # race byte on the wire
    # stat_cap + followers
    buf.write_u16(225)  # stat_cap
    buf.write_u8(2)     # followers
    buf.write_u8(5)     # followers_max
    # AOS block (type >= 4, and 5 >= 4)
    buf.write_u16(11)   # fire
    buf.write_u16(12)   # cold
    buf.write_u16(13)   # poison
    buf.write_u16(14)   # energy
    buf.write_u16(300)  # luck
    buf.write_u16(7)    # damage_min
    buf.write_u16(19)   # damage_max
    buf.write_u32(99)   # tithing points

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_character_status_ml_decodes_gender_and_race():
    """Regression: the gender byte and the type>=5 race byte were skipped,
    so is_female/race were never populated. The whole ML tail (weight_max,
    stat_cap, resists, luck/damage) must also stay byte-aligned.
    """
    h, p, w = _make_stack()
    serial = p.self_state.serial  # _make_stack uses 0x00000001

    # Elf female (wire race byte 2 = RaceID 1 + 1).
    h.dispatch(0x11, _build_status_ml(serial, female=True, race=2))

    ss = p.self_state
    assert ss.is_female is True
    assert ss.race == 2
    # The type>=5 weight_max comes off the wire, not the STR fallback.
    assert ss.weight_max == 510
    # Tail stays aligned after consuming gender + weight_max + race.
    assert ss.stat_cap == 225
    assert ss.followers == 2
    assert ss.resist_fire == 11
    assert ss.luck == 300
    assert ss.damage_max == 19

    # A non-ML-enabled account emits race byte 0; coerce to Human(1).
    h.dispatch(0x11, _build_status_ml(serial, female=False, race=0))
    assert ss.is_female is False
    assert ss.race == 1


# ---------------------------------------------------------------------------
# WorldItem (0x1A) — ground item field-order regression
# ---------------------------------------------------------------------------


def _build_world_item(
    serial: int,
    graphic: int,
    *,
    amount: int | None,
    graphic_inc: int | None,
    x: int,
    y: int,
    z: int,
    hue: int | None,
) -> bytes:
    """Craft a 0x1A WorldItem packet on the wire.

    Mirrors ClassicUO PacketHandlers.UpdateItem ordering:
    serial -> graphic -> [graphic_inc] -> [amount u16] -> x -> y ->
    [direction] -> z -> [hue].
    """
    buf = PacketWriter()
    buf.write_u8(0x1A)
    buf.write_u16(0)  # length placeholder

    wire_serial = serial | 0x80000000 if amount is not None else serial
    buf.write_u32(wire_serial)

    wire_graphic = graphic | 0x8000 if graphic_inc is not None else graphic
    buf.write_u16(wire_graphic)
    if graphic_inc is not None:
        buf.write_u8(graphic_inc)
    if amount is not None:
        buf.write_u16(amount)

    buf.write_u16(x)  # no direction flag
    wire_y = y | 0x8000 if hue is not None else y
    buf.write_u16(wire_y)
    buf.write_i8(z)
    if hue is not None:
        buf.write_u16(hue)

    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_world_item_stack_and_extended_graphic_decode():
    """Regression: a ground item with BOTH the stack-amount flag (serial high
    bit) and an extended graphic (0x8000) must decode correctly. The old code
    read the amount before the graphic_inc byte, misaligning every following
    field. Order must match ClassicUO: graphic -> graphic_inc -> amount -> x.
    """
    h, p, w = _make_stack()
    serial = 0x40001234

    data = _build_world_item(
        serial,
        graphic=0x0E76,
        amount=23,
        graphic_inc=3,
        x=1500,
        y=2600,
        z=-7,
        hue=0x0042,
    )
    h.dispatch(0x1A, data)

    item = p.world.items.get(serial)
    assert item is not None
    assert item.amount == 23
    assert item.graphic == 0x0E76 + 3  # base + graphic_inc
    assert item.x == 1500
    assert item.y == 2600
    assert item.z == -7
    assert item.hue == 0x0042


def test_world_item_plain_no_flags():
    """A simple unstacked ground item (no flags) still decodes correctly."""
    h, p, w = _make_stack()
    serial = 0x40005678

    data = _build_world_item(
        serial,
        graphic=0x1F1C,
        amount=None,
        graphic_inc=None,
        x=100,
        y=200,
        z=5,
        hue=None,
    )
    h.dispatch(0x1A, data)

    item = p.world.items.get(serial)
    assert item is not None
    assert item.amount == 1  # defaults to 1 when no stack flag
    assert item.graphic == 0x1F1C
    assert item.x == 100
    assert item.y == 200
    assert item.z == 5
    assert item.hue == 0


def test_world_item_multi_strips_0x4000_flag():
    """Regression: a ground multi (BaseMulti) is sent by ServUO with its art id
    OR'd with 0x4000 (`itemID | 0x4000`). That bit is a "this is a multi" flag,
    not part of the graphic number — ClassicUO's UpdateItem treats it as a multi
    and uses the low bits as the real art id. The handler must mask 0x4000 off so
    item.graphic is the true art id, not art_id + 0x4000.
    """
    h, p, w = _make_stack()
    serial = 0x40009ABC
    base_graphic = 0x0064  # a small-multi art id

    # Build a 0x1A WorldItem whose graphic carries the multi flag (no stack,
    # no graphic_inc, no hue/flags). Mirrors ServUO WorldItem ordering:
    # serial -> graphic(|0x4000) -> x -> y -> z.
    buf = PacketWriter()
    buf.write_u8(0x1A)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)  # no stack-amount high bit
    buf.write_u16(base_graphic | 0x4000)  # multi flag set
    buf.write_u16(300)  # x, no direction flag
    buf.write_u16(400)  # y, no hue/flags bits
    buf.write_i8(0)  # z
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))

    h.dispatch(0x1A, bytes(data))

    item = p.world.items.get(serial)
    assert item is not None
    # The multi flag must be stripped — graphic is the real art id.
    assert item.graphic == base_graphic
    assert item.graphic & 0x4000 == 0
    assert item.x == 300
    assert item.y == 400
    assert item.z == 0
    assert item.amount == 1


# ---------------------------------------------------------------------------
# MobileIncoming (0x78) — NewMobileIncoming equipment record alignment
# ---------------------------------------------------------------------------


def _build_mobile_incoming(serial: int, equipment: list[tuple[int, int, int, int]]) -> bytes:
    """Build a 0x78 NewMobileIncoming packet.

    `equipment` is a list of (item_serial, graphic, layer, hue). Each worn item
    is a fixed record: serial(u32)+graphic(u16)+layer(u8)+hue(u16), with the hue
    ALWAYS present — matching ServUO's MobileIncoming (protocol 70331+).
    """
    buf = PacketWriter()
    buf.write_u8(0x78)
    buf.write_u16(0)       # length placeholder
    buf.write_u32(serial)
    buf.write_u16(0x0190)  # body
    buf.write_u16(1000)    # x
    buf.write_u16(2000)    # y
    buf.write_i8(10)       # z
    buf.write_u8(2)        # direction
    buf.write_u16(0x0000)  # hue
    buf.write_u8(0x00)     # flags
    buf.write_u8(1)        # notoriety
    for item_serial, graphic, layer, hue in equipment:
        buf.write_u32(item_serial)
        buf.write_u16(graphic)
        buf.write_u8(layer)
        buf.write_u16(hue)
    buf.write_u32(0)       # terminator
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_mobile_incoming_equipment_always_has_hue():
    """ServUO NewMobileIncoming writes a fixed serial+graphic+layer+hue record.

    The decoder must consume the hue for EVERY item (no 0x8000 flag gate), or
    the 2 unread hue bytes desync the next item's serial and corrupt the rest
    of the equipment list.
    """
    h, p, w = _make_stack()

    mob_serial = 0x00000099
    # Two ordinary items (graphic < 0x8000) each carrying a real hue, followed
    # by a third. Under the old flag-gated decode the first item's hue bytes
    # would be misread as the second item's serial, corrupting items 2 and 3.
    equipment = [
        (0x40000001, 0x1F03, 0x02, 0x0021),  # robe, layer 2, hue 0x21
        (0x40000002, 0x13B9, 0x01, 0x0455),  # sword, layer 1, hue 0x455
        (0x40000003, 0x1515, 0x16, 0x0000),  # cloak, layer 0x16, hue 0
    ]
    data = _build_mobile_incoming(mob_serial, equipment)
    h.dispatch(0x78, data)

    # Exactly the three declared items must exist with correct fields, and no
    # garbage serials from a misaligned read.
    for item_serial, graphic, layer, hue in equipment:
        item = p.world.items.get(item_serial)
        assert item is not None, f"missing item 0x{item_serial:08X}"
        assert item.graphic == graphic
        assert item.layer == layer
        assert item.hue == hue
        assert item.container == mob_serial

    # No spurious items beyond the three we sent (no desync garbage).
    equip_items = [it for it in p.world.items.values() if it.container == mob_serial]
    assert len(equip_items) == len(equipment)


# ---------------------------------------------------------------------------
# Damage (0x0B) — combat-result decode + zero-damage (parry) gating
# ---------------------------------------------------------------------------


def _damage_packet(serial: int, amount: int) -> bytes:
    buf = PacketWriter()
    buf.write_u8(0x0B)
    buf.write_u32(serial)
    buf.write_u16(amount)
    return buf.to_bytes()


def test_damage_taken_decodes_amount_and_stamps_window():
    h, p, w = _make_stack()

    h.dispatch(0x0B, _damage_packet(0x00000001, 17))  # self serial

    assert p.self_state.last_damage_taken_at > 0.0
    events = p.poll_events()
    taken = [e for e in events if e.type == GameEventType.DAMAGE_TAKEN]
    assert len(taken) == 1
    assert taken[0].data["amount"] == 17


def test_damage_dealt_decodes_serial_and_amount():
    h, p, w = _make_stack()

    h.dispatch(0x0B, _damage_packet(0x00000099, 23))  # other serial

    events = p.poll_events()
    dealt = [e for e in events if e.type == GameEventType.DAMAGE_DEALT]
    assert len(dealt) == 1
    assert dealt[0].data == {"serial": 0x00000099, "amount": 23}


def test_zero_damage_self_does_not_reset_combat_window():
    """A fully-parried (amount=0) swing must not look like a real hit.

    MeleeAttack defensive mode re-engages based on last_damage_taken_at, so
    a 0-damage 0x0B must leave that timestamp (and the event stream) alone,
    matching ClassicUO's Damage handler which only acts when damage > 0.
    """
    h, p, w = _make_stack()
    p.self_state.last_damage_taken_at = 0.0

    h.dispatch(0x0B, _damage_packet(0x00000001, 0))  # self serial, parried

    assert p.self_state.last_damage_taken_at == 0.0
    events = p.poll_events()
    assert not any(e.type == GameEventType.DAMAGE_TAKEN for e in events)


def test_zero_damage_other_emits_no_event():
    h, p, w = _make_stack()

    h.dispatch(0x0B, _damage_packet(0x00000099, 0))  # other serial, absorbed

    events = p.poll_events()
    assert not any(e.type == GameEventType.DAMAGE_DEALT for e in events)


def _build_delete(serial: int) -> bytes:
    """Build a 0x1D Delete packet ([id][serial:u32], 5 bytes)."""
    return struct.pack(">BI", 0x1D, serial)


def test_delete_clears_worn_equipment_layer():
    """0x1D Delete of a worn item must clear its self_state.equipment layer."""
    h, p, _ = _make_stack()
    weapon_serial = 0x40005678
    backpack_serial = 0x40009999
    # Layer 1 = right hand (one-handed weapon), 0x15 = backpack.
    p.self_state.equipment[1] = weapon_serial
    p.self_state.equipment[0x15] = backpack_serial

    h.dispatch(0x1D, _build_delete(weapon_serial))

    # The weapon's hand layer is gone, so equip guards see an empty hand.
    assert 1 not in p.self_state.equipment
    # Unrelated layers (the backpack) are untouched.
    assert p.self_state.equipment.get(0x15) == backpack_serial


def test_delete_unrelated_item_leaves_equipment_intact():
    """Deleting an item we are NOT wearing must not disturb equipment."""
    h, p, _ = _make_stack()
    p.self_state.equipment[1] = 0x40001111
    h.dispatch(0x1D, _build_delete(0x4000DEAD))
    assert p.self_state.equipment.get(1) == 0x40001111
