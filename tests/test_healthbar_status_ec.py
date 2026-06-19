"""Tests for the EC 0x16 NewHealthbarUpdate variant.

ClassicUO registers BOTH 0x16 and 0x17 to the same NewHealthbarUpdate handler
with an identical wire layout, and (for CV_500A+ clients) treats 0x16 as a
variable-length packet. ServUO emits the 0x16 EC variants
(HealthbarPoisonEC / HealthbarYellowEC) to Enhanced-Client sessions. These tests
pin (a) 0x16 is declared variable-length so framing never desyncs, and (b) it
routes through the same poison/yellow-bar logic as 0x17.
"""

import struct

from anima.client.codec import PacketWriter
from anima.client.handler import PacketHandler
from anima.client.packets import get_packet_length
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _healthbar_packet(packet_id: int, serial: int, status_type: int, flag: int) -> bytes:
    """Build a single-entry healthbar packet under the given id (0x16 or 0x17)."""
    buf = PacketWriter()
    buf.write_u8(packet_id)
    buf.write_u16(0)  # length placeholder
    buf.write_u32(serial)
    buf.write_u16(1)  # count
    buf.write_u16(status_type)
    buf.write_u8(flag)
    data = bytearray(buf.to_bytes())
    data[1:3] = struct.pack(">H", len(data))
    return bytes(data)


def test_0x16_is_variable_length():
    # ClassicUO PacketsTable.cs flips 0x16 from fixed 1 to variable (-1) for
    # CV_500A+ clients. Anima reports 7.0.102.3, so 0x16 must be variable (0):
    # a fixed length of 1 would consume only the id byte and desync the stream.
    assert get_packet_length(0x16) == 0
    assert get_packet_length(0x16) == get_packet_length(0x17)


def test_0x16_poison_routes_through_same_handler():
    h, p, w = _make_stack()
    serial = 0x00000099
    p.world.get_or_create_mobile(serial)

    # status_type 1 = poison, flag = level + 1 (flag 3 -> level 2 / Greater)
    h.dispatch(0x16, _healthbar_packet(0x16, serial, status_type=1, flag=3))

    assert p.world.mobiles[serial].is_poisoned is True
    assert p.world.mobiles[serial].poison_level == 2


def test_0x16_poison_on_self():
    h, p, w = _make_stack()
    self_serial = p.self_state.serial

    h.dispatch(0x16, _healthbar_packet(0x16, self_serial, status_type=1, flag=2))
    assert p.self_state.is_poisoned is True
    assert p.self_state.poison_level == 1

    h.dispatch(0x16, _healthbar_packet(0x16, self_serial, status_type=1, flag=0))
    assert p.self_state.is_poisoned is False
    assert p.self_state.poison_level == -1


def test_0x16_yellow_bar_does_not_touch_poison_flag():
    h, p, w = _make_stack()
    serial = 0x00000099
    mob = p.world.get_or_create_mobile(serial)
    mob.is_poisoned = True

    # status_type 2 = yellow/blessed bar — must not clear the poison flag
    h.dispatch(0x16, _healthbar_packet(0x16, serial, status_type=2, flag=1))

    assert p.world.mobiles[serial].is_poisoned is True
