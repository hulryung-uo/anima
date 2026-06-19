"""0x1D Delete must invalidate a cached open_container / context_menu serial.

Regression: SelfState caches the last-opened container serial (set by 0x24
ContainerDisplay) and the last context-menu target. World.remove only drops
world.items / world.mobiles, so when the container is destroyed (a looted
corpse despawning, a bag emptied, a bank box closed server-side) the cached
serial dangled. _wait_for_bank_box treats a non-zero open_container that
isn't the backpack as "bank is open", so a stale corpse serial made the
banking flow target a gone container and report the bank ready falsely.
"""

from anima.client.codec import PacketWriter
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.walker import WalkerManager
from anima.perception.handlers import register_handlers


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _delete(h: PacketHandler, serial: int) -> None:
    buf = PacketWriter()
    buf.write_u8(0x1D)
    buf.write_u32(serial)
    h.dispatch(0x1D, buf.to_bytes())


def test_delete_clears_matching_open_container():
    h, p, w = _make_stack()
    corpse = 0x40005678
    p.world.get_or_create_item(corpse)
    p.self_state.open_container = corpse

    _delete(h, corpse)

    assert corpse not in p.world.items
    assert p.self_state.open_container == 0


def test_delete_clears_matching_context_menu_target():
    h, p, w = _make_stack()
    target = 0x4000ABCD
    p.world.get_or_create_mobile(target)
    p.self_state.context_menu_serial = target
    p.self_state.context_menu = ["Open Trade", "Buy"]

    _delete(h, target)

    assert p.self_state.context_menu_serial == 0
    assert p.self_state.context_menu == []


def test_delete_leaves_other_open_container_intact():
    """A delete for an unrelated entity must not wipe the open container."""
    h, p, w = _make_stack()
    bank = 0x40001111
    other = 0x40002222
    p.world.get_or_create_item(bank)
    p.world.get_or_create_item(other)
    p.self_state.open_container = bank

    _delete(h, other)

    assert p.self_state.open_container == bank
