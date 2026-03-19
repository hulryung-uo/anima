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
