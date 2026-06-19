"""0x25 AddItemToContainer must clear a worn item from self_state.equipment.

Regression: disarming / swapping a weapon drags the worn item from a hand
layer into the backpack. The server sends 0x25 (the item still exists — NOT a
0x1D Delete), re-parenting it to the bag. The 0x25 handler used to update only
item.container, leaving self_state.equipment[layer] pointing at the now-in-pack
serial. The combat re-arm guards (ss.equipment.get(1)/.get(2)) then saw a
phantom worn weapon and never re-equipped, so the agent fought bare-handed.

ClassicUO clears this in UpdateContainedItem via World.RemoveItemFromContainer.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

ONE_HANDED_LAYER = 1
BACKPACK_LAYER = 0x15


def _make_stack() -> tuple[PacketHandler, Perception]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _equip_on_self(h: PacketHandler, serial: int, layer: int, parent: int) -> None:
    """Dispatch a 0x2E EquipItem so the item lands in self_state.equipment."""
    pkt = bytearray()
    pkt.append(0x2E)
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x13B9)  # weapon graphic
    pkt.append(0)  # graphic increment (signed)
    pkt.append(layer)
    pkt += struct.pack(">I", parent)
    pkt += struct.pack(">H", 0)  # hue
    h.dispatch(0x2E, bytes(pkt))


def _add_to_container(h: PacketHandler, serial: int, container: int) -> None:
    """Dispatch a 0x25 AddItemToContainer (fixed length 21)."""
    pkt = bytearray()
    pkt.append(0x25)
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x13B9)  # graphic
    pkt.append(0)  # graphic increment
    pkt += struct.pack(">H", 1)  # amount
    pkt += struct.pack(">H", 0)  # x
    pkt += struct.pack(">H", 0)  # y
    pkt.append(0)  # grid index
    pkt += struct.pack(">I", container)
    pkt += struct.pack(">H", 0)  # hue
    h.dispatch(0x25, bytes(pkt))


def test_worn_weapon_dragged_to_backpack_clears_equipment_slot():
    h, p = _make_stack()
    weapon = 0x40001234
    backpack = 0x40000099

    # The weapon starts worn on the one-handed layer.
    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # Drag it into the backpack — the server reports the move via 0x25, NOT a
    # 0x1D Delete (the item still exists).
    _add_to_container(h, weapon, container=backpack)

    # The hand layer must no longer claim the weapon, so the combat re-arm
    # guard sees an empty hand.
    assert ONE_HANDED_LAYER not in p.self_state.equipment
    assert not p.self_state.equipment.get(1)
    # The item itself is now tracked inside the backpack.
    assert p.world.items[weapon].container == backpack


def test_unrelated_container_add_does_not_touch_equipment():
    h, p = _make_stack()
    weapon = 0x40001234
    loot = 0x4000AAAA
    backpack = 0x40000099

    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # A different item dropping into the bag must leave the worn weapon intact.
    _add_to_container(h, loot, container=backpack)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon
