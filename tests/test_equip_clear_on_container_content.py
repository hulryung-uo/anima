"""0x3C ContainerContent must clear a worn item re-parented into a container.

Regression / coherence gap: the single-add 0x25 AddItemToContainer, the 0x1D
Delete, and the 0x2E re-equip-elsewhere handlers all drop a stranded
self_state.equipment slot when a serial we were wearing leaves our paperdoll.
The bulk-refresh sibling 0x3C ContainerContent did NOT — it only updated
item.container. So when a disarm/swap is reported via a container refresh (the
backpack re-opened after the swap, or the server pushing a fresh content list
with the now-in-pack weapon), self_state.equipment[layer] kept pointing at the
now-in-pack serial. The combat re-arm guards (ss.equipment.get(1)/.get(2)) and
equip_weapon_from_pack then saw a phantom worn weapon and never re-armed.

ClassicUO clears this in UpdateContainedItem via World.RemoveItemFromContainer,
regardless of whether the move arrived as a single add or a bulk refresh.
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


def _container_content(h: PacketHandler, entries: list[tuple[int, int]]) -> None:
    """Dispatch a 0x3C ContainerContent (variable length) listing ``entries``.

    Each entry is ``(item_serial, container_serial)``; the per-item record is
    the 20-byte ServUO layout the handler parses.
    """
    body = bytearray()
    body += struct.pack(">H", len(entries))  # count
    for serial, container in entries:
        body += struct.pack(">I", serial)
        body += struct.pack(">H", 0x13B9)  # graphic
        body.append(0)  # graphic increment
        body += struct.pack(">H", 1)  # amount
        body += struct.pack(">H", 0)  # x
        body += struct.pack(">H", 0)  # y
        body.append(0)  # grid index
        body += struct.pack(">I", container)
        body += struct.pack(">H", 0)  # hue
    pkt = bytearray()
    pkt.append(0x3C)
    pkt += struct.pack(">H", len(body) + 3)  # total length incl id + length
    pkt += body
    h.dispatch(0x3C, bytes(pkt))


def test_worn_weapon_in_container_refresh_clears_equipment_slot():
    h, p = _make_stack()
    weapon = 0x40001234
    backpack = 0x40000099

    # The weapon starts worn on the one-handed layer.
    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # A container refresh now lists the weapon inside the backpack — the disarm
    # was reported via 0x3C, not a single 0x25 or a 0x1D Delete.
    _container_content(h, [(weapon, backpack)])

    # The hand layer must no longer claim the weapon, so the combat re-arm
    # guard sees an empty hand.
    assert ONE_HANDED_LAYER not in p.self_state.equipment
    assert not p.self_state.equipment.get(1)
    # The item itself is now tracked inside the backpack.
    assert p.world.items[weapon].container == backpack


def test_unrelated_container_refresh_does_not_touch_equipment():
    h, p = _make_stack()
    weapon = 0x40001234
    loot = 0x4000AAAA
    backpack = 0x40000099

    _equip_on_self(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon

    # A refresh listing only an unrelated loot item must leave the worn weapon
    # intact in the equipment map.
    _container_content(h, [(loot, backpack)])
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon
