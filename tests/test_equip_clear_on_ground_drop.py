"""0x1A WorldItem / 0xF3 UpdateItemSA must clear a worn item from equipment.

Regression: when a worn weapon/shield leaves the paperdoll onto the GROUND — a
server-side Disarm special move, or the agent dropping a hand item to the floor
rather than into a bag — ServUO reports it via 0x1A WorldItem (or 0xF3 on an
EC/HS session). The item still exists, so NO 0x1D Delete arrives. The ground
handlers re-parented the item (container = 0) but left self_state.equipment
pointing at the now-on-ground serial, so the combat re-arm guards
(ss.equipment.get(1)/.get(2)) saw a phantom worn weapon and never re-equipped.

The 0x25 / 0x2E / 0x3C / 0x1D handlers already guard this; these two were the
missing siblings. ClassicUO drops a worn item off the paperdoll when it
re-enters the world list at no owner.
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


def _world_item_on_ground(h: PacketHandler, serial: int) -> None:
    """Dispatch a 0x1A WorldItem (variable length) for a ground item.

    Minimal layout: serial(u32, no stack/has-amount bit) + graphic(u16, no
    0x8000 ext bit) + x(u16) + y(u16) + z(i8). No hue/flags bits set.
    """
    body = bytearray()
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0x13B9)  # graphic (< 0x8000, < 0x4000)
    body += struct.pack(">H", 1000)  # x
    body += struct.pack(">H", 1000)  # y
    body += struct.pack(">b", 0)  # z
    pkt = bytearray()
    pkt.append(0x1A)
    pkt += struct.pack(">H", len(body) + 3)  # variable length field
    pkt += body
    h.dispatch(0x1A, bytes(pkt))


def _world_item_sa(h: PacketHandler, serial: int) -> None:
    """Dispatch a 0xF3 UpdateItemSA (fixed) for a ground item."""
    pkt = bytearray()
    pkt.append(0xF3)
    pkt += struct.pack(">H", 0)  # unknown
    pkt.append(0x00)  # data_type 0x00 = ordinary item
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x13B9)  # graphic
    pkt.append(0)  # graphic_inc
    pkt += struct.pack(">H", 1)  # amount
    pkt += struct.pack(">H", 1)  # amount again
    pkt += struct.pack(">H", 1000)  # x
    pkt += struct.pack(">H", 1000)  # y
    pkt += struct.pack(">b", 0)  # z
    pkt.append(0)  # light/direction
    pkt += struct.pack(">H", 0)  # hue
    pkt.append(0)  # flags
    h.dispatch(0xF3, bytes(pkt))


def test_world_item_drop_clears_worn_equipment_slot():
    h, p = _make_stack()
    weapon = 0x40001234

    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # The weapon is force-disarmed onto the ground — reported via 0x1A, not a
    # 0x1D Delete (the item still exists in the world).
    _world_item_on_ground(h, weapon)

    # The hand layer must no longer claim the weapon.
    assert ONE_HANDED_LAYER not in p.self_state.equipment
    assert not p.self_state.equipment.get(1)
    # The item is now a ground item (no parent container).
    assert p.world.items[weapon].container == 0


def test_world_item_sa_drop_clears_worn_equipment_slot():
    h, p = _make_stack()
    weapon = 0x40005678

    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    _world_item_sa(h, weapon)

    assert ONE_HANDED_LAYER not in p.self_state.equipment
    assert not p.self_state.equipment.get(1)
    assert p.world.items[weapon].container == 0


def test_unrelated_ground_item_does_not_touch_equipment():
    h, p = _make_stack()
    weapon = 0x40001234
    loot = 0x4000AAAA

    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # A different item landing on the ground must leave the worn weapon intact.
    _world_item_on_ground(h, loot)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon
