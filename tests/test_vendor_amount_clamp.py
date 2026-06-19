"""Tests for vendor buy/sell amount clamping in the 0x3B / 0x9F builders.

ServUO reads the per-item amount as a *signed* Int16 and drops any item
whose amount is <= 0:

  * VendorBuyReply  (Server/Network/PacketHandlers.cs): ``int amount =
    pvSrc.ReadInt16();`` then ``if (amount <= 0) continue;``
  * VendorSellReply (same file): ``int Amount = pvSrc.ReadInt16();`` then
    only ``Amount > 0`` items are added to the sell list.

PacketWriter.write_u16 masks with ``& 0xFFFF`` and never raises, so an
amount >= 32768 wraps to a negative value server-side and the item is
silently dropped. The builders must clamp each amount into [1, 32767].
"""

from __future__ import annotations

import struct

import pytest

from anima.client.packets import build_buy_items, build_sell_items

# Largest positive signed Int16 — the value ServUO can read without wrap.
_MAX = 0x7FFF


def _buy_amounts(data: bytes) -> list[int]:
    """Extract the per-item u16 amounts from a 0x3B BuyItems packet."""
    # [0]=id [1:3]=len [3:7]=vendor [7]=flag, then 7-byte entries:
    # [0]=0x1A layer [1:5]=serial [5:7]=amount
    assert data[0] == 0x3B
    body = data[8:]
    amounts: list[int] = []
    for off in range(0, len(body), 7):
        entry = body[off : off + 7]
        assert entry[0] == 0x1A
        amounts.append(struct.unpack_from(">H", entry, 5)[0])
    return amounts


def _sell_amounts(data: bytes) -> list[int]:
    """Extract the per-item u16 amounts from a 0x9F SellItems packet."""
    # [0]=id [1:3]=len [3:7]=vendor [7:9]=count, then 6-byte entries:
    # [0:4]=serial [4:6]=amount
    assert data[0] == 0x9F
    count = struct.unpack_from(">H", data, 7)[0]
    body = data[9:]
    amounts: list[int] = []
    for i in range(count):
        off = i * 6
        amounts.append(struct.unpack_from(">H", body, off + 4)[0])
    return amounts


@pytest.mark.parametrize("raw", [32768, 40000, 65535, 70000])
def test_buy_amount_above_signed_max_is_clamped(raw: int) -> None:
    data = build_buy_items(0xDEADBEEF, [(0x12345678, raw)])
    (amount,) = _buy_amounts(data)
    # Must be clamped to the largest positive signed Int16, never wrapped.
    assert amount == _MAX
    # And it must remain positive when ServUO re-reads it as signed.
    assert struct.unpack(">h", struct.pack(">H", amount))[0] > 0


@pytest.mark.parametrize("raw", [32768, 40000, 65535, 70000])
def test_sell_amount_above_signed_max_is_clamped(raw: int) -> None:
    data = build_sell_items(0xDEADBEEF, [(0x12345678, raw)])
    (amount,) = _sell_amounts(data)
    assert amount == _MAX
    assert struct.unpack(">h", struct.pack(">H", amount))[0] > 0


@pytest.mark.parametrize("raw", [0, -5])
def test_buy_amount_below_one_is_clamped_up(raw: int) -> None:
    data = build_buy_items(0xDEADBEEF, [(0x12345678, raw)])
    (amount,) = _buy_amounts(data)
    assert amount == 1


@pytest.mark.parametrize("raw", [0, -5])
def test_sell_amount_below_one_is_clamped_up(raw: int) -> None:
    data = build_sell_items(0xDEADBEEF, [(0x12345678, raw)])
    (amount,) = _sell_amounts(data)
    assert amount == 1


@pytest.mark.parametrize("raw", [1, 3, 100, 32767])
def test_in_range_amounts_pass_through_unchanged(raw: int) -> None:
    buy = build_buy_items(0xDEADBEEF, [(0x11111111, raw)])
    sell = build_sell_items(0xDEADBEEF, [(0x11111111, raw)])
    assert _buy_amounts(buy) == [raw]
    assert _sell_amounts(sell) == [raw]


def test_multiple_items_each_clamped_independently() -> None:
    items = [(0x1, 5), (0x2, 90000), (0x3, 0), (0x4, 32767)]
    assert _sell_amounts(build_sell_items(0xAB, items)) == [5, _MAX, 1, 32767]
    assert _buy_amounts(build_buy_items(0xAB, items)) == [5, _MAX, 1, 32767]


def test_sell_count_field_unaffected_by_clamp() -> None:
    # The u16 count must still reflect the number of items, and the
    # 6-byte-per-entry size invariant ServUO checks must hold.
    items = [(0x1, 90000), (0x2, 90000)]
    data = build_sell_items(0xAB, items)
    count = struct.unpack_from(">H", data, 7)[0]
    assert count == 2
    # ServUO asserts: size == 1 + 2 + 4 + 2 + count * 6
    assert len(data) == 1 + 2 + 4 + 2 + count * 6
