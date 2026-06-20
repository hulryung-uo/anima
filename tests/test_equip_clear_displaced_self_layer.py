"""0x2E EquipItem replacing our OWN layer must strip the displaced item.

Regression (sibling of the 0x25 / 0x1D / 0x3C / 0x2E-reparent clears): when we
equip a NEW serial onto a layer we already occupy, the server reports it via a
single 0x2E with parent == self. The handler overwrote self_state.equipment
[layer], but the OLD item kept living in world.items with container == self and
its old layer > 0. The equipment-recovery fallback in main.py
(`it.container == serial and it.layer > 0`) then rebuilds equipment[layer] from
that orphan, walking the just-replaced weapon back onto the paperdoll so the
combat re-arm guards see a phantom worn weapon.

ClassicUO's EquipItem removes the previous item from the owner/layer before
adding the new one.
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


def _recovery_fallback(p: Perception) -> None:
    """Replay main.py's equipment-recovery scan over world.items."""
    for it in p.world.items.values():
        if it.container == p.self_state.serial and it.layer > 0:
            p.self_state.equipment[it.layer] = it.serial


def test_self_equip_replace_strips_displaced_item():
    h, p = _make_stack()
    old_weapon = 0x40001111
    new_weapon = 0x40002222

    # Start with the old weapon worn on our one-handed layer.
    _equip(h, old_weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == old_weapon

    # Equip a different weapon onto the SAME layer (single 0x2E, no 0x1D Delete).
    _equip(h, new_weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == new_weapon

    # The displaced old weapon must no longer masquerade as worn-on-self, so the
    # recovery fallback cannot promote it back onto the paperdoll.
    old_item = p.world.items.get(old_weapon)
    assert old_item is not None
    assert old_item.container != p.self_state.serial
    assert old_item.layer == 0

    _recovery_fallback(p)
    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == new_weapon


def test_self_equip_same_serial_is_noop_for_displacement():
    """Re-sending 0x2E for the SAME serial/layer must not nuke its own state."""
    h, p = _make_stack()
    weapon = 0x40001111

    _equip(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)
    _equip(h, weapon, ONE_HANDED_LAYER, parent=p.self_state.serial)

    assert p.self_state.equipment.get(ONE_HANDED_LAYER) == weapon
    item = p.world.items.get(weapon)
    assert item is not None
    assert item.container == p.self_state.serial
    assert item.layer == ONE_HANDED_LAYER
