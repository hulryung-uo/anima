"""0x2E EquipItem onto ANOTHER mobile must clear the item from our equipment.

Regression (sibling of the 0x25 / 0x1D clears): when an item we were wearing is
re-equipped on a different mobile, the server reports it via 0x2E with parent !=
self (the item still exists, so there is NO 0x1D Delete). The 0x2E handler used
to only ADD to self_state.equipment when parent == self, never removing on a
re-parent away from us. self_state.equipment[layer] then kept pointing at the
departed serial, so the combat re-arm guards saw a phantom worn weapon and the
agent fought bare-handed.

ClassicUO's EquipItem moves the item off the previous owner's layer slot.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

ONE_HANDED_LAYER = 1


def _make_stack() -> tuple[PacketHandler, Perception]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _equip(h: PacketHandler, serial: int, layer: int, parent: int) -> None:
    """Dispatch a 0x2E EquipItem record."""
    pkt = bytearray()
    pkt.append(0x2E)
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x13B9)  # weapon graphic
    pkt.append(0)  # graphic increment (signed)
    pkt.append(layer)
    pkt += struct.pack(">I", parent)
    pkt += struct.pack(">H", 0)  # hue
    h.dispatch(0x2E, bytes(pkt))


def test_reequip_onto_other_mobile_clears_our_slot():
    h, p = _make_stack()
    weapon = 0x40001234
    thief = 0x000ABCDE  # some other mobile

    # The weapon starts worn on our one-handed layer.
    _equip(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # The same serial is now equipped on a DIFFERENT mobile (parent != self),
    # reported via 0x2E — no 0x1D Delete.
    _equip(h, weapon, ONE_HANDED_LAYER, parent=thief)

    # Our hand layer must no longer claim the departed weapon.
    assert ONE_HANDED_LAYER not in p.self_state.equipment
    assert not p.self_state.equipment.get(1)
    # The item is now tracked as worn on the other mobile.
    assert p.world.items[weapon].container == thief


def test_reequip_of_unrelated_serial_leaves_our_loadout_intact():
    h, p = _make_stack()
    our_weapon = 0x40001234
    foe_weapon = 0x4000AAAA
    foe = 0x000ABCDE

    _equip(h, our_weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == our_weapon

    # A foe equipping its OWN (different) weapon must not disturb our slot.
    _equip(h, foe_weapon, ONE_HANDED_LAYER, parent=foe)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == our_weapon
