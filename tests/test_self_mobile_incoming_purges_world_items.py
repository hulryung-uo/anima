"""A self 0x78 worn-item refresh must purge stale worn items from world.items,
not just from self_state.equipment.

Regression: handle_mobile_incoming's self branch cleared the equipment dict on
a full refresh but left our own worn items lingering in world.items with
container == self.serial (the gear item gets no paired 0x1D Delete on a gear
swap). The non-self branch already purges those orphans; the self branch did
not. main.py's equipment-recovery fallback rebuilds
``equipment[it.layer] = it.serial`` from ``it.container == self.serial and
it.layer > 0`` — so the lingering orphan resurrects the dropped weapon into
equipment[1]/[2], the exact phantom the refresh purge exists to kill.
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


def test_self_refresh_purges_dropped_worn_item_from_world_items():
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    two_hander = (0x40000010, 0x13FB, 1, 0)   # layer 1 = OneHanded
    backpack = (0x40000012, 0x0E75, 0x15, 0)  # layer 0x15 = Backpack

    # First refresh: armed with the weapon.
    h.dispatch(0x78, _build_mobile_incoming(self_serial, [two_hander, backpack]))
    assert p.self_state.equipment.get(1) == two_hander[0]
    # The weapon is now an item in world.items, parented to us.
    worn = p.world.items.get(two_hander[0])
    assert worn is not None
    assert worn.container == self_serial
    assert worn.layer == 1

    # Second refresh: hands now empty (weapon dropped/swapped) — server omits
    # layer 1, keeps only the backpack. No paired 0x1D Delete for the weapon.
    h.dispatch(0x78, _build_mobile_incoming(self_serial, [backpack]))

    # Equipment dict cleared (already guarded elsewhere)...
    assert p.self_state.equipment.get(1) is None
    # ...AND the stale worn item is gone from world.items, so it can't be
    # resurrected by the main.py equipment-recovery fallback.
    assert two_hander[0] not in p.world.items
    # The backpack (inventory root) survives in both stores.
    assert p.self_state.equipment.get(0x15) == backpack[0]
    assert backpack[0] in p.world.items


def test_self_refresh_fallback_scan_cannot_resurrect_dropped_weapon():
    """Reproduce the concrete harm: scanning world.items for our worn gear
    (the main.py recovery fallback) must NOT re-add the dropped weapon's layer
    after a refresh that omitted it."""
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    weapon = (0x40000010, 0x13B9, 1, 0)
    backpack = (0x40000012, 0x0E75, 0x15, 0)

    h.dispatch(0x78, _build_mobile_incoming(self_serial, [weapon, backpack]))
    h.dispatch(0x78, _build_mobile_incoming(self_serial, [backpack]))

    # Mirror main.py's fallback: rebuild equipment from worn items in the world.
    rebuilt: dict[int, int] = {}
    for it in p.world.items.values():
        if it.container == self_serial and it.layer > 0:
            rebuilt[it.layer] = it.serial

    # The dropped weapon must not reappear at the hand layer.
    assert 1 not in rebuilt
    assert rebuilt.get(0x15) == backpack[0]


def test_self_refresh_keeps_a_still_worn_item():
    """A layer present in the new refresh is refilled, not lost to the purge."""
    self_serial = 0x00000001
    h, p, _ = _make_stack(self_serial)

    chest = (0x40000013, 0x1C04, 13, 0)  # layer 13 = Torso
    backpack = (0x40000012, 0x0E75, 0x15, 0)

    h.dispatch(0x78, _build_mobile_incoming(self_serial, [chest, backpack]))
    # A second refresh that still carries the chest keeps it in both stores.
    h.dispatch(0x78, _build_mobile_incoming(self_serial, [chest, backpack]))

    assert p.self_state.equipment.get(13) == chest[0]
    assert chest[0] in p.world.items
    assert p.world.items[chest[0]].container == self_serial
