"""0x11 CharacterStatus: stat_cap / damage_min / damage_max decode signed.

ServUO's MobileStatus packet (Server/Network/Packets.cs) writes StatCap,
the weapon damage range, and every resist as a signed `(short)` on the wire,
and ClassicUO's CharacterStatus handler reads them back signed. A negative
value (stat-loss curse, a damage debuff) must stay negative rather than wrap
to ~65500 when decoded as an unsigned u16.
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


def _build_status_type6(
    *,
    serial: int,
    stat_cap: int,
    damage_min: int,
    damage_max: int,
) -> bytes:
    """Frame a type-6 (ExtendedStatus) self 0x11 packet.

    Mirrors ServUO MobileStatus field order so the resist block and the
    type-6 tail stay aligned exactly as the live shard sends it.
    """
    body = bytearray()
    body += struct.pack(">I", serial)
    body += b"Anima".ljust(30, b"\x00")  # name (30 ascii)
    body += struct.pack(">H", 80)        # hits
    body += struct.pack(">H", 100)       # hits_max
    body += struct.pack(">B", 0)         # name-change flag (renamable)
    body += struct.pack(">B", 6)         # type = 6 (ExtendedStatus)

    # type > 0 core block
    body += struct.pack(">B", 1)         # female
    body += struct.pack(">H", 90)        # str
    body += struct.pack(">H", 70)        # dex
    body += struct.pack(">H", 50)        # int
    body += struct.pack(">H", 40)        # stam
    body += struct.pack(">H", 70)        # stam_max
    body += struct.pack(">H", 30)        # mana
    body += struct.pack(">H", 50)        # mana_max
    body += struct.pack(">I", 1234)      # gold
    body += struct.pack(">h", 25)        # physical resist / armor (signed)
    body += struct.pack(">H", 200)       # weight

    # type >= 5: weight_max + race
    body += struct.pack(">H", 355)       # weight_max
    body += struct.pack(">B", 1)         # race (Human)

    # stat_cap + followers (signed stat_cap under test)
    body += struct.pack(">h", stat_cap)  # stat_cap (signed on the wire)
    body += struct.pack(">B", 1)         # followers
    body += struct.pack(">B", 5)         # followers_max

    # type >= 4: resists, luck, damage range, tithing
    body += struct.pack(">h", 30)        # resist_fire
    body += struct.pack(">h", 25)        # resist_cold
    body += struct.pack(">h", 20)        # resist_poison
    body += struct.pack(">h", 35)        # resist_energy
    body += struct.pack(">H", 0)         # luck (unsigned)
    body += struct.pack(">h", damage_min)  # damage_min (signed)
    body += struct.pack(">h", damage_max)  # damage_max (signed)
    body += struct.pack(">I", 0)         # tithing points

    # type >= 6: 15 AOS-status shorts (i in 0..=14) — trailing tail
    for _ in range(15):
        body += struct.pack(">h", 0)

    pkt = bytearray()
    pkt.append(0x11)
    pkt += struct.pack(">H", 0)          # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_negative_damage_and_stat_cap_decode_signed():
    h, p, w = _make_stack()

    pkt = _build_status_type6(
        serial=0x00000001,
        stat_cap=-50,
        damage_min=-3,
        damage_max=-1,
    )
    assert h.dispatch(0x11, pkt)

    # Signed decode: a negative debuff stays negative instead of wrapping.
    assert p.self_state.stat_cap == -50
    assert p.self_state.damage_min == -3
    assert p.self_state.damage_max == -1

    # The surrounding contiguous block must still be framed correctly.
    assert p.self_state.strength == 90
    assert p.self_state.weight == 200
    assert p.self_state.weight_max == 355
    assert p.self_state.followers == 1
    assert p.self_state.followers_max == 5
    assert p.self_state.resist_fire == 30
    assert p.self_state.resist_energy == 35


def test_positive_damage_and_stat_cap_unchanged():
    h, p, w = _make_stack()

    pkt = _build_status_type6(
        serial=0x00000001,
        stat_cap=225,
        damage_min=11,
        damage_max=19,
    )
    assert h.dispatch(0x11, pkt)

    assert p.self_state.stat_cap == 225
    assert p.self_state.damage_min == 11
    assert p.self_state.damage_max == 19
