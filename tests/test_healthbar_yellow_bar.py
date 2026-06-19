"""Tests for the 0x17 yellow/blessed bar (status_type 2) decode.

ServUO sends the yellow/blessed health bar as a SEPARATE single-entry packet
(HealthbarYellow, Packets.cs:3817): count=1, status_type=2, flag=1 when
m.Blessed || m.YellowHealthbar else 0. Before the fix the handler returned
early on any non-poison packet, so this dynamic invulnerable signal — the same
one ClassicUO exposes as Mobile.IsYellowHits — never reached WorldState.
"""

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


def test_yellow_bar_sets_flag_on_mobile():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    assert mob.is_yellow_health is False

    # status_type 2 = yellow/blessed bar, flag 1 = set
    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=1))

    assert p.world.mobiles[serial].is_yellow_health is True


def test_yellow_bar_cleared_on_mobile():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    mob.is_yellow_health = True

    # flag 0 = no longer blessed/yellow
    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=0))

    assert p.world.mobiles[serial].is_yellow_health is False


def test_yellow_bar_on_self():
    h, p, w = _make_stack()
    self_serial = p.self_state.serial

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=2, flag=1))
    assert p.self_state.is_yellow_health is True

    h.dispatch(0x17, _healthbar_packet(self_serial, status_type=2, flag=0))
    assert p.self_state.is_yellow_health is False


def test_yellow_packet_does_not_clobber_poison():
    # The yellow packet carries no poison entry; it must leave is_poisoned and
    # poison_level untouched (separate ServUO packets never overwrite each
    # other's field).
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=4))  # level 3
    assert mob.is_poisoned is True
    assert mob.poison_level == 3

    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=1))
    assert mob.is_yellow_health is True
    assert mob.is_poisoned is True  # unchanged
    assert mob.poison_level == 3  # unchanged


def test_poison_packet_does_not_clobber_yellow():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    h.dispatch(0x17, _healthbar_packet(serial, status_type=2, flag=1))
    assert mob.is_yellow_health is True

    h.dispatch(0x17, _healthbar_packet(serial, status_type=1, flag=0))  # cured
    assert mob.is_poisoned is False
    assert mob.is_yellow_health is True  # unchanged
