"""Tests for 0x17 HealthBarStatusUpdate poison tracking."""

import struct

from anima.client.codec import PacketWriter
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


def _healthbar_packet(serial: int, status_type: int, flag: int) -> bytes:
    """Build a single-entry 0x17 packet (ServUO HealthbarPoison/Yellow)."""
    buf = PacketWriter()
    buf.write_u8(0x17)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_u16(1)  # count
    buf.write_u16(status_type)
    buf.write_u8(flag)
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_poison_status_sets_flag_on_mobile():
    h, p, w = _make_stack()
    serial = 0x00000099
    p.world.get_or_create_mobile(serial)

    # status_type 1 = poison, flag = level+1 (here level 2 -> flag 3)
    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=3))

    assert p.world.mobiles[serial].is_poisoned is True


def test_poison_cured_clears_flag_on_mobile():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    mob.is_poisoned = True

    # flag 0 = cured
    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=0))

    assert p.world.mobiles[serial].is_poisoned is False


def test_poison_status_for_unknown_mobile_creates_no_phantom():
    """A 0x17 status update must NEVER create a mobile.

    The 0x16/0x17 NewHealthbarUpdate packet carries no position, so a
    poison/yellow-bar update for a serial not (yet / no longer) in view used to
    spawn a positionless phantom MobileInfo at (0,0) — the same bug the
    0xA1 / 0x2D / 0x11 handlers already guard. ClassicUO's NewHealthbarUpdate
    returns when ``world.Mobiles.Get(serial) == null``; only the spatial
    packets (0x78/0x77/0x20) create mobiles. Lock that invariant.
    """
    h, p, w = _make_stack()
    serial = 0x000000AB
    assert serial not in p.world.mobiles

    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=2))

    # No phantom mobile spawned — nothing parked at the map origin.
    assert serial not in p.world.mobiles
    assert p.world.nearby_mobiles(0, 0) == []


def test_yellow_bar_for_unknown_mobile_creates_no_phantom():
    """Same guard for the status_type 2 (yellow/blessed) branch."""
    h, p, w = _make_stack()
    serial = 0x000000CD
    assert serial not in p.world.mobiles

    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=1))

    assert serial not in p.world.mobiles


def test_poison_status_refreshes_already_seen_mobile_last_seen():
    """The guard only blocks CREATION: an existing foe is still updated, and
    its last_seen is re-stamped so the status stream keeps it fresh against
    prune_stale_mobiles (a stationary foe issues no 0x77 to refresh it)."""
    import time as _t

    h, p, w = _make_stack()
    serial = 0x000000EF
    mob = p.world.get_or_create_mobile(serial)
    mob.x, mob.y = 100, 200
    mob.last_seen = 1000.0  # last touched long ago

    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=3))

    assert p.world.mobiles[serial].is_poisoned is True
    assert p.world.mobiles[serial].last_seen > 1000.0
    assert serial not in p.world.prune_stale_mobiles(now=_t.monotonic(), max_age=30.0)
    assert serial in p.world.mobiles


def test_poison_status_on_self():
    h, p, w = _make_stack()
    self_serial = p.self_state.serial

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=1, flag=4))
    assert p.self_state.is_poisoned is True

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=1, flag=0))
    assert p.self_state.is_poisoned is False


def test_yellow_bar_does_not_touch_poison_flag():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    mob.is_poisoned = True

    # status_type 2 = yellow/blessed bar — must not clear the poison flag
    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=1))

    assert p.world.mobiles[serial].is_poisoned is True


def test_truncated_packet_is_ignored():
    h, p, w = _make_stack()
    # Too short to contain serial + count — must not raise.
    short = bytes([0x17, 0x00, 0x05, 0x00, 0x00])
    h.dispatch(0x17, short)


def test_poison_level_decoded_on_mobile():
    # ServUO sends flag = Poison.Level + 1 (0=Lesser .. 4=Lethal).
    # flag 3 -> level 2 (Greater). The boolean must stay True alongside it.
    h, p, w = _make_stack()
    serial = 0x00000099
    p.world.get_or_create_mobile(serial)

    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=3))

    assert p.world.mobiles[serial].is_poisoned is True
    assert p.world.mobiles[serial].poison_level == 2


def test_poison_level_lethal_vs_lesser_distinguished():
    # The whole point of tracking level: a Lethal (flag 5 -> level 4) poison
    # must be distinguishable from a Lesser (flag 1 -> level 0) one, even
    # though both set is_poisoned True.
    h, p, w = _make_stack()
    lesser, lethal = 0x00000010, 0x00000011
    p.world.get_or_create_mobile(lesser)
    p.world.get_or_create_mobile(lethal)

    h.dispatch(0x17, _healthbar_packet(lesser, status_type=1, flag=1))
    h.dispatch(0x17, _healthbar_packet(lethal, status_type=1, flag=5))

    assert p.world.mobiles[lesser].poison_level == 0
    assert p.world.mobiles[lethal].poison_level == 4
    assert p.world.mobiles[lesser].is_poisoned is True
    assert p.world.mobiles[lethal].is_poisoned is True


def test_cure_clears_poison_level_on_mobile():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=4))
    assert mob.poison_level == 3

    # flag 0 = cured -> level -1, boolean False
    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=0))
    assert mob.poison_level == -1
    assert mob.is_poisoned is False


def test_poison_level_on_self():
    h, p, w = _make_stack()
    self_serial = p.self_state.serial

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=1, flag=2))
    assert p.self_state.is_poisoned is True
    assert p.self_state.poison_level == 1

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=1, flag=0))
    assert p.self_state.is_poisoned is False
    assert p.self_state.poison_level == -1
