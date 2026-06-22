"""0x1D Delete must invalidate cached vendor trading state.

Regression: SelfState caches the vendor mobile serial plus its buy/sell lists
(0x74 / 0x9E). handle_close_vendor (0x3B) clears them when the vendor window
closes, but a vendor mobile can leave view via 0x1D Delete with NO 0x3B (walks
out of range, the agent recalls away, the vendor despawns). The buy/sell flows
poll a non-empty list as the "vendor ready" signal and then send buy/sell
packets to vendor_serial, so a stale list for a gone vendor makes the next
trade target the wrong (departed) vendor. Mirror the open_container clear.
"""

from anima.client.codec import PacketWriter
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.walker import WalkerManager
from anima.perception.handlers import register_handlers
from anima.perception.self_state import VendorBuyItem, VendorSellItem


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


def test_delete_clears_vendor_state_for_departed_vendor():
    h, p, w = _make_stack()
    vendor = 0x40009999
    p.world.get_or_create_mobile(vendor)
    p.self_state.vendor_serial = vendor
    p.self_state.vendor_buy_list = [
        VendorBuyItem(serial=0x1, graphic=0x1BF2, amount=1, price=8, name="iron ingot")
    ]
    p.self_state.vendor_sell_list = [
        VendorSellItem(serial=0x2, graphic=0x0F0E, amount=1, price=3, name="empty bottle")
    ]

    _delete(h, vendor)

    assert vendor not in p.world.mobiles
    assert p.self_state.vendor_serial == 0
    assert p.self_state.vendor_buy_list == []
    assert p.self_state.vendor_sell_list == []


def test_delete_leaves_other_vendor_state_intact():
    """A delete for an unrelated entity must not wipe the tracked vendor."""
    h, p, w = _make_stack()
    vendor = 0x40009999
    other = 0x40001234
    p.world.get_or_create_mobile(vendor)
    p.world.get_or_create_mobile(other)
    p.self_state.vendor_serial = vendor
    buy = [VendorBuyItem(serial=0x1, graphic=0x1BF2, amount=1, price=8, name="iron ingot")]
    p.self_state.vendor_buy_list = buy

    _delete(h, other)

    assert p.self_state.vendor_serial == vendor
    assert p.self_state.vendor_buy_list == buy


def test_delete_non_vendor_does_not_touch_zero_vendor_serial():
    """With no vendor tracked, deleting any entity leaves vendor state at zero."""
    h, p, w = _make_stack()
    item = 0x40005678
    p.world.get_or_create_item(item)
    assert p.self_state.vendor_serial == 0

    _delete(h, item)

    assert p.self_state.vendor_serial == 0
    assert p.self_state.vendor_buy_list == []
    assert p.self_state.vendor_sell_list == []
