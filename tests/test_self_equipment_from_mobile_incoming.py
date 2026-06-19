"""0x78 MobileIncoming for our own character must populate self_state.equipment.

Regression: handle_mobile_incoming returned early for the self serial,
dropping the worn-item list that ServUO appends to the player's own 0x78.
ClassicUO's UpdateObject parses that item loop for world.Player too, so
self_state.equipment was left empty (or stale after a gear swap) and the
equip guards mis-judged whether a hand was occupied.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.walker import WalkerManager
from anima.perception.handlers import register_handlers


def _make_stack(player_serial: int = 0x00000001):
    p = Perception(player_serial=player_serial)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_mobile_incoming(
    serial: int,
    body: int,
    x: int,
    y: int,
    z: int,
    direction: int,
    hue: int,
    flags: int,
    notoriety: int,
    equipment: list[tuple[int, int, int, int]],
) -> bytes:
    """Build a 0x78 (NewMobileIncoming / CV_70331) packet.

    Each equipment tuple is (item_serial, graphic, layer, item_hue); the
    hue is ALWAYS present in this protocol level. The list is terminated by
    a zero serial.
    """
    body_buf = bytearray()
    body_buf += struct.pack(">I", serial)
    body_buf += struct.pack(">H", body)
    body_buf += struct.pack(">H", x)
    body_buf += struct.pack(">H", y)
    body_buf += struct.pack(">b", z)
    body_buf += struct.pack(">B", direction)
    body_buf += struct.pack(">H", hue)
    body_buf += struct.pack(">B", flags)
    body_buf += struct.pack(">B", notoriety)
    for item_serial, graphic, layer, item_hue in equipment:
        body_buf += struct.pack(">I", item_serial)
        body_buf += struct.pack(">H", graphic)
        body_buf += struct.pack(">B", layer)
        body_buf += struct.pack(">H", item_hue)
    body_buf += struct.pack(">I", 0)  # terminator

    pkt = bytearray()
    pkt.append(0x78)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body_buf
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_self_mobile_incoming_populates_equipment():
    """Our own 0x78 fills self_state.equipment from the worn-item list."""
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    weapon = (0x40000010, 0x13B9, 1, 0)   # layer 1 = OneHanded
    chest = (0x40000011, 0x1C04, 13, 0)   # layer 13 = Torso
    pkt = _build_mobile_incoming(
        serial=self_serial,
        body=0x0190,
        x=100, y=200, z=0,
        direction=2, hue=0, flags=0, notoriety=1,
        equipment=[weapon, chest],
    )
    h.dispatch(0x78, pkt)

    # Before the fix this stayed empty because the handler returned early.
    assert p.self_state.equipment.get(1) == weapon[0]
    assert p.self_state.equipment.get(13) == chest[0]
    # Self must never leak into world.mobiles.
    assert self_serial not in p.world.mobiles


def test_other_mobile_incoming_still_tracked():
    """A non-self 0x78 still lands in world.mobiles and doesn't touch self equipment."""
    self_serial = 0x00000001
    other = 0x00001234
    h, p, _ = _make_stack(self_serial)

    pkt = _build_mobile_incoming(
        serial=other,
        body=0x0033,
        x=50, y=60, z=5,
        direction=4, hue=0x03B2, flags=0, notoriety=3,
        equipment=[(0x40000020, 0x1F03, 1, 0)],
    )
    h.dispatch(0x78, pkt)

    assert other in p.world.mobiles
    mob = p.world.mobiles[other]
    assert mob.body == 0x0033
    assert mob.x == 50 and mob.y == 60 and mob.z == 5
    assert p.self_state.equipment == {}
    # The worn item is attributed to the other mobile, not to self.
    assert p.world.items[0x40000020].container == other
