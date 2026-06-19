"""is_yellow_health must track the 0x08 mobile-flags bit (blessed / yellow bar).

ServUO's Mobile.GetPacketFlags sets bit 0x08 for `m_Blessed || m_YellowHealthbar`
and ships it in every 0x78 MobileIncoming / 0x77 MobileMoving / 0x20 MobileUpdate
flags byte. ClassicUO derives Mobile.IsYellowHits straight from `Flags &
Flags.YellowBar` (EntityFlags.cs / Mobile.cs). A statically Blessed mobile (e.g.
the resurrection-spot Healer NPC) NEVER emits a standalone 0x17 HealthbarYellow —
that only fires when the YellowHealthbar *property* toggles — so before this fix
is_yellow_health stayed stuck at False for blessed mobiles, and beneficial.py
could not tell an invulnerable/blessed target apart from a healable one.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.enums import MobileFlags
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

YELLOW_BAR = 0x08


def _make_stack(player_serial: int = 0x00000001):
    p = Perception(player_serial=player_serial)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _mobile_incoming(serial: int, flags: int, *, notoriety: int = 1) -> bytes:
    body = bytearray()
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0x0190)  # body
    body += struct.pack(">H", 100)     # x
    body += struct.pack(">H", 200)     # y
    body += struct.pack(">b", 0)       # z
    body += struct.pack(">B", 2)       # direction
    body += struct.pack(">H", 0)       # hue
    body += struct.pack(">B", flags)
    body += struct.pack(">B", notoriety)
    body += struct.pack(">I", 0)       # equipment terminator
    pkt = bytearray([0x78])
    pkt += struct.pack(">H", 0)
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def _mobile_moving(serial: int, flags: int, *, notoriety: int = 1) -> bytes:
    pkt = bytearray([0x77])
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x0190)  # body
    pkt += struct.pack(">H", 100)     # x
    pkt += struct.pack(">H", 200)     # y
    pkt += struct.pack(">b", 0)       # z
    pkt += struct.pack(">B", 2)       # direction
    pkt += struct.pack(">H", 0)       # hue
    pkt += struct.pack(">B", flags)
    pkt += struct.pack(">B", notoriety)
    return bytes(pkt)


def test_self_0x78_blessed_bit_sets_is_yellow_health():
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)
    assert p.self_state.is_yellow_health is False

    # Server blesses us: flag 0x08 arrives in the self 0x78, with NO 0x17.
    h.dispatch(0x78, _mobile_incoming(self_serial, YELLOW_BAR))
    assert p.self_state.is_yellow_health is True
    assert bool(p.self_state.flags & MobileFlags.YELLOW_BAR) is True

    # A later 0x78 without the bit clears it (blessing removed).
    h.dispatch(0x78, _mobile_incoming(self_serial, 0x00))
    assert p.self_state.is_yellow_health is False


def test_self_0x77_moving_tracks_yellow_bar():
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    h.dispatch(0x77, _mobile_moving(self_serial, YELLOW_BAR))
    assert p.self_state.is_yellow_health is True

    h.dispatch(0x77, _mobile_moving(self_serial, 0x00))
    assert p.self_state.is_yellow_health is False


def test_other_mobile_0x78_blessed_bit_sets_is_yellow_health():
    self_serial = 0x00000001
    npc_serial = 0x00001000
    h, p, _ = _make_stack(self_serial)

    # A blessed NPC (e.g. a town/resurrection healer) appears with bit 0x08 and
    # never a 0x17 — its yellow bar must still be visible to the brain.
    h.dispatch(0x78, _mobile_incoming(npc_serial, YELLOW_BAR, notoriety=7))
    mob = p.world.mobiles[npc_serial]
    assert mob.is_yellow_health is True


def test_yellow_bar_bit_does_not_disturb_other_self_flags():
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    # War mode + hidden + yellow bar all set together in one flags byte.
    combo = int(MobileFlags.WAR_MODE) | int(MobileFlags.HIDDEN) | YELLOW_BAR
    h.dispatch(0x78, _mobile_incoming(self_serial, combo))

    assert p.self_state.is_yellow_health is True
    assert p.self_state.in_war_mode is True
    assert p.self_state.hidden is True
