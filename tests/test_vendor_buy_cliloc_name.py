"""0x74 VendorBuyList: a numeric-cliloc item name must resolve to its string.

ServUO's GenericBuyInfo names a stock item ``(1020000 + itemID).ToString()``
when it has no custom Name (Scripts/VendorInfo/GenericBuy.cs:65-66), so the
name field in the 0x74 packet commonly arrives as a bare cliloc id like
"1020386". ClassicUO's BuyList handler (PacketHandlers.cs:2758) resolves it via
``int.TryParse -> Clilocs.Translate``. The handler must do the same — exactly
as the 0x9E sell list already does — so the buy flow's name-based reporting
sees a readable label, not raw digits.

The 0x74 packet carries only prices+names; the actual items (with their
graphics) arrive first via 0x3C ContainerContent, which the handler correlates
by (x, y) order. So each test seeds the container via a 0x3C packet, then sends
the 0x74 price/name list.
"""

from __future__ import annotations

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

VENDOR = 0x10000001
BUY_CONTAINER = 0x40000099


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_container_content(
    container: int, items: list[tuple[int, int, int]]
) -> bytes:
    """Build a 0x3C ContainerContent packet.

    Each item tuple is (serial, graphic, index). ServUO writes the buy
    container in REVERSE with x=(index+1), y=1; the handler sorts by (x, y),
    so we set x=index+1, y=1 to recover the canonical order.
    Per-item layout (handler): serial u32, graphic u16, graphic_inc u8,
    amount u16, x u16, y u16, grid_index u8, container u32, hue u16 = 20 bytes.
    """
    body = bytearray()
    body += struct.pack(">H", len(items))
    for serial, graphic, index in items:
        body += struct.pack(">I", serial)
        body += struct.pack(">H", graphic)
        body += struct.pack(">B", 0)  # graphic_inc
        body += struct.pack(">H", 1)  # amount
        body += struct.pack(">H", index + 1)  # x
        body += struct.pack(">H", 1)  # y
        body += struct.pack(">B", 0)  # grid_index
        body += struct.pack(">I", container)
        body += struct.pack(">H", 0)  # hue
    pkt = bytearray()
    pkt.append(0x3C)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def _build_buy_list(container: int, entries: list[tuple[int, str]]) -> bytes:
    """Build a 0x74 VendorBuyList packet.

    Each entry is (price, name). ServUO writes name_len = len+1 followed by a
    NUL-terminated string (WriteAsciiNull); read_ascii(name_len) consumes the
    NUL as the last counted byte. Layout: container u32, count u8, then per
    item: price u32, name_len u8, name ascii + NUL.
    """
    body = bytearray()
    body += struct.pack(">I", container)
    body += struct.pack(">B", len(entries))
    for price, name in entries:
        body += struct.pack(">I", price)
        encoded = name.encode("ascii") + b"\x00"
        body += struct.pack(">B", len(encoded))
        body += encoded
    pkt = bytearray()
    pkt.append(0x74)
    pkt += struct.pack(">H", 0)  # length placeholder
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def _seed_container(h: PacketHandler, p: Perception) -> None:
    """Place the buy container itself under the vendor, then fill it."""
    cont = p.world.get_or_create_item(BUY_CONTAINER)
    cont.container = VENDOR
    h.dispatch(
        0x3C,
        _build_container_content(
            BUY_CONTAINER,
            [
                (0x40000010, 0x1BF2, 0),  # iron ingot
                (0x40000011, 0x0E86, 1),  # a pickaxe
            ],
        ),
    )


def test_numeric_cliloc_name_is_resolved(monkeypatch) -> None:
    """A digits-only buy-list name that maps to a cliloc resolves to the label."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(
        handlers_mod,
        "cliloc_text",
        lambda num: {1020386: "iron ingot"}.get(num, ""),
    )
    h, p, _ = _make_stack()
    _seed_container(h, p)
    h.dispatch(0x74, _build_buy_list(BUY_CONTAINER, [(5, "1020386"), (37, "a pickaxe")]))

    by_graphic = {it.graphic: it for it in p.self_state.vendor_buy_list}
    assert by_graphic[0x1BF2].name == "iron ingot"
    assert by_graphic[0x1BF2].price == 5
    # A non-numeric name is left verbatim, never run through cliloc lookup.
    assert by_graphic[0x0E86].name == "a pickaxe"


def test_unresolvable_numeric_name_falls_back_to_digits(monkeypatch) -> None:
    """If the cliloc id has no string, keep the literal rather than blanking."""
    import anima.perception.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "cliloc_text", lambda num: "")
    h, p, _ = _make_stack()
    _seed_container(h, p)
    h.dispatch(0x74, _build_buy_list(BUY_CONTAINER, [(5, "9999999"), (37, "a pickaxe")]))

    by_graphic = {it.graphic: it for it in p.self_state.vendor_buy_list}
    assert by_graphic[0x1BF2].name == "9999999"
    assert by_graphic[0x0E86].name == "a pickaxe"
