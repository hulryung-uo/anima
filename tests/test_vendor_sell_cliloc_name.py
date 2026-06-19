"""0x9E VendorSellList: a numeric-cliloc item name must resolve to its string.

ServUO's GenericSellInfo.GetNameFor (Scripts/VendorInfo/GenericSell.cs) returns
``item.LabelNumber.ToString()`` for any item without a custom Name, so the name
field in the 0x9E packet commonly arrives as a bare cliloc id like "1027154".
ClassicUO's SellList handler (PacketHandlers.cs) resolves it via
``int.TryParse -> Clilocs.GetString``. The handler must do the same so the
sell flow's name-based item matching sees a readable label, not raw digits.
"""

from __future__ import annotations

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_sell_list(vendor: int, items: list[tuple[int, int, int, int, int, str]]) -> bytes:
    """Build a 0x9E VendorSellList packet.

    Each item tuple is (serial, graphic, hue, amount, price, name).
    Layout per ServUO Packets.cs:419-441:
      serial u32, graphic u16, hue u16, amount u16, price u16,
      name_len u16, name ascii.
    """
    body = bytearray()
    body += struct.pack(">I", vendor)
    body += struct.pack(">H", len(items))
    for serial, graphic, hue, amount, price, name in items:
        body += struct.pack(">IHHHH", serial, graphic, hue, amount, price)
        encoded = name.encode("ascii")
        body += struct.pack(">H", len(encoded))
        body += encoded
    pkt = bytearray()
    pkt.append(0x9E)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_numeric_cliloc_name_is_resolved(monkeypatch) -> None:
    """A digits-only name that maps to a cliloc string resolves to the label."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(
        handlers_mod,
        "cliloc_text",
        lambda num: {1027154: "iron ingot"}.get(num, ""),
    )
    h, p, _ = _make_stack()
    pkt = _build_sell_list(
        0xDEADBEEF,
        [(0x40000001, 0x1BF2, 0, 50, 3, "1027154")],
    )
    h.dispatch(0x9E, pkt)

    (item,) = p.self_state.vendor_sell_list
    assert item.name == "iron ingot"
    assert item.amount == 50
    assert item.price == 3


def test_custom_named_item_is_left_verbatim(monkeypatch) -> None:
    """A real (non-numeric) name must never be run through cliloc lookup."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "cliloc_text", lambda num: "WRONG")
    h, p, _ = _make_stack()
    pkt = _build_sell_list(
        0xDEADBEEF,
        [(0x40000002, 0x0EED, 0, 1, 100, "a sack of gold")],
    )
    h.dispatch(0x9E, pkt)

    (item,) = p.self_state.vendor_sell_list
    assert item.name == "a sack of gold"


def test_unresolvable_numeric_name_falls_back_to_digits(monkeypatch) -> None:
    """If the cliloc id has no string, keep the literal rather than blanking."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "cliloc_text", lambda num: "")
    h, p, _ = _make_stack()
    pkt = _build_sell_list(
        0xDEADBEEF,
        [(0x40000003, 0x1234, 0, 2, 5, "9999999")],
    )
    h.dispatch(0x9E, pkt)

    (item,) = p.self_state.vendor_sell_list
    assert item.name == "9999999"


def test_mixed_list_resolves_each_independently(monkeypatch) -> None:
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(
        handlers_mod,
        "cliloc_text",
        lambda num: {1015101: "scissors", 1024140: "a pickaxe"}.get(num, ""),
    )
    h, p, _ = _make_stack()
    pkt = _build_sell_list(
        0xDEADBEEF,
        [
            (0x40000004, 0x0F9F, 0, 1, 7, "1015101"),
            (0x40000005, 0x0E86, 0, 1, 11, "a fine blade"),
            (0x40000006, 0x0E85, 0, 1, 13, "1024140"),
        ],
    )
    h.dispatch(0x9E, pkt)

    names = [it.name for it in p.self_state.vendor_sell_list]
    assert names == ["scissors", "a fine blade", "a pickaxe"]
