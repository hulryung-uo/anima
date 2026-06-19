"""A self 0x78 is a full loadout refresh: layers absent from the new worn-item
list must be dropped from self_state.equipment, mirroring ClassicUO's
UpdateObject (which strips every worn item off the mobile except the backpack
before replaying the item loop).

Regression: handle_mobile_incoming only *appended* to self_state.equipment, so
after a gear change that REMOVED a layer (a dropped one-hander, or a two-hander
replacing a 1H + shield) without a paired 0x1D Delete, the old serial lingered
in equipment[1]/[2]. The combat-loop "hand already occupied / already equipped"
guards then saw a phantom weapon and never re-armed.
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
    equipment: list[tuple[int, int, int, int]],
    *,
    body: int = 0x0190,
    x: int = 100,
    y: int = 200,
    z: int = 0,
    direction: int = 2,
    hue: int = 0,
    flags: int = 0,
    notoriety: int = 1,
) -> bytes:
    """Build a 0x78 (NewMobileIncoming / CV_70331) packet."""
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


def test_self_mobile_incoming_drops_layers_absent_from_refresh():
    """A weapon worn in one 0x78 is cleared when the next self 0x78 omits it."""
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    one_hander = (0x40000010, 0x13B9, 1, 0)   # layer 1 = OneHanded
    shield = (0x40000011, 0x1B72, 2, 0)       # layer 2 = TwoHanded slot (shield)
    backpack = (0x40000012, 0x0E75, 0x15, 0)  # layer 0x15 = Backpack
    chest = (0x40000013, 0x1C04, 13, 0)       # layer 13 = Torso

    # First refresh: armed with a one-hander + shield, wearing a backpack.
    h.dispatch(
        0x78,
        _build_mobile_incoming(self_serial, [one_hander, shield, backpack, chest]),
    )
    assert p.self_state.equipment.get(1) == one_hander[0]
    assert p.self_state.equipment.get(2) == shield[0]
    assert p.self_state.equipment.get(0x15) == backpack[0]

    # Second refresh: hands are now empty (e.g. weapon dropped to draw a fresh
    # one) — the server omits layers 1 and 2 but keeps the backpack + chest.
    h.dispatch(
        0x78,
        _build_mobile_incoming(self_serial, [backpack, chest]),
    )

    # Both hand layers must be gone — not left pointing at the dropped serials.
    assert p.self_state.equipment.get(1) is None
    assert p.self_state.equipment.get(2) is None
    # The backpack (inventory root the rest of the code relies on) survives.
    assert p.self_state.equipment.get(0x15) == backpack[0]
    assert p.self_state.equipment.get(13) == chest[0]


def test_self_mobile_incoming_preserves_backpack_when_loadout_empties():
    """An empty worn-item list still keeps the backpack layer."""
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    backpack = (0x40000012, 0x0E75, 0x15, 0)
    weapon = (0x40000010, 0x13B9, 1, 0)

    h.dispatch(0x78, _build_mobile_incoming(self_serial, [backpack, weapon]))
    assert p.self_state.equipment.get(1) == weapon[0]

    # A bare refresh that carries only the backpack must clear the weapon layer
    # but never the backpack root.
    h.dispatch(0x78, _build_mobile_incoming(self_serial, [backpack]))
    assert p.self_state.equipment.get(1) is None
    assert p.self_state.equipment.get(0x15) == backpack[0]
