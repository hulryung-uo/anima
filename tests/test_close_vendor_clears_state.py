"""0x3B CloseVendorInterface must invalidate the cached vendor lists.

Regression: handle_close_vendor only logged. The buy/sell procedures poll a
non-empty vendor_buy_list / vendor_sell_list as the "list ready" signal, so a
stale list left over from a just-closed vendor makes the next vendor open race
against the wrong (closed) vendor_serial.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.self_state import VendorBuyItem, VendorSellItem
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _build_close_vendor(serial: int) -> bytes:
    """[0x3B][len:u16][serial:u32]."""
    pkt = bytearray()
    pkt.append(0x3B)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += struct.pack(">I", serial)
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def _seed_vendor(p: Perception, serial: int) -> None:
    p.self_state.vendor_serial = serial
    p.self_state.vendor_buy_list = [
        VendorBuyItem(serial=0xA, graphic=0xF7B, amount=20, price=3, name="reagent")
    ]
    p.self_state.vendor_sell_list = [
        VendorSellItem(serial=0xB, graphic=0x1BF2, amount=5, price=4, name="ingot")
    ]


def test_close_vendor_clears_matching_vendor_state():
    h, p = _make_stack()
    vendor = 0x40001234
    _seed_vendor(p, vendor)

    h.dispatch(0x3B, _build_close_vendor(vendor))

    assert p.self_state.vendor_serial == 0
    assert p.self_state.vendor_buy_list == []
    assert p.self_state.vendor_sell_list == []


def test_close_vendor_ignores_stale_close_for_other_vendor():
    """A late 0x3B for an old vendor must NOT wipe a freshly-opened one."""
    h, p = _make_stack()
    current = 0x40009999
    _seed_vendor(p, current)

    # Close arrives for a different (previously-open) vendor.
    h.dispatch(0x3B, _build_close_vendor(0x40001234))

    assert p.self_state.vendor_serial == current
    assert len(p.self_state.vendor_buy_list) == 1
    assert len(p.self_state.vendor_sell_list) == 1
