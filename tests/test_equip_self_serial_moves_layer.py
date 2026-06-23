"""0x2E EquipItem moving the SAME self-worn serial to a new layer must clear
the prior layer (an item has exactly one layer — no double-slot phantom).

Regression (sibling of test_equip_clear_displaced_self_layer, which only covers
a DIFFERENT serial replacing the OLD one on the SAME layer): when the server
re-reports a serial we already wear on layer A as now worn on layer B (a 1H
weapon re-slotted, a ring moved), the handler overwrote equipment[B] but left
the stale equipment[A] == serial entry. The same serial then occupied TWO slots,
so the combat re-arm guards (ss.equipment.get(1)/.get(2)) saw a now-free hand as
still armed. ClassicUO's EquipItem moves the item off its previous layer first.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

LAYER_A = 1  # one-handed
LAYER_B = 2  # two-handed / off-hand


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
    pkt.append(0)  # signed graphic increment
    pkt.append(layer)
    pkt += struct.pack(">I", parent)
    pkt += struct.pack(">H", 0)  # hue
    h.dispatch(0x2E, bytes(pkt))


def test_self_serial_moves_layer_clears_prior_slot():
    h, p = _make_stack()
    weapon = 0x40001111

    # Worn on layer A.
    _equip(h, weapon, LAYER_A, parent=p.self_state.serial)
    assert p.self_state.equipment.get(LAYER_A) == weapon

    # The SAME serial is re-equipped onto layer B (single 0x2E, no 0x1D Delete).
    _equip(h, weapon, LAYER_B, parent=p.self_state.serial)

    # It now lives ONLY on layer B — the abandoned layer A slot must be gone, so
    # the same serial never occupies two equipment slots at once.
    assert p.self_state.equipment.get(LAYER_B) == weapon
    assert LAYER_A not in p.self_state.equipment
    assert list(p.self_state.equipment.values()).count(weapon) == 1


def test_self_equip_same_serial_same_layer_is_noop():
    """Re-sending 0x2E for the SAME serial AND layer must not drop the slot."""
    h, p = _make_stack()
    weapon = 0x40001111

    _equip(h, weapon, LAYER_A, parent=p.self_state.serial)
    _equip(h, weapon, LAYER_A, parent=p.self_state.serial)

    assert p.self_state.equipment.get(LAYER_A) == weapon
    item = p.world.items.get(weapon)
    assert item is not None
    assert item.container == p.self_state.serial
    assert item.layer == LAYER_A
