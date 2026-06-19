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
