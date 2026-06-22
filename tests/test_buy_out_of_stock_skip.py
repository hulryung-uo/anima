"""BuyFromVendor must skip out-of-stock (amount==0) listings at selection.

REGRESSION: a vendor buy list can carry a listing whose current stock has
drained to 0 (a competing buyer cleaned it out, or the restock timer has not
ticked). ServUO's BaseVendor.OnBuyItems clamps delivery to current stock and
delivers nothing for a 0-stock line. The selection loops only gated on
price/affordability, so the 0-stock line could be picked: ``_buy_quantity``
floors the order to 1, the doomed buy fires, and the failure path trips the
10-minute ``_buy_disabled_until`` cooldown — starving the whole tool-restock
loop even though an in-stock affordable tool sat right next to it on the list.

These tests exercise the pure ``_in_stock`` gate and prove the priority-loop
selection now jumps the 0-stock listing to the next in-stock one.
"""
from __future__ import annotations

from types import SimpleNamespace

import anima.procedures.buy_from_vendor as bv
from anima.procedures.buy_from_vendor import _in_stock


def _item(serial, graphic, price, amount):
    return SimpleNamespace(
        serial=serial, graphic=graphic, price=price, amount=amount, name="tool"
    )


# A pickaxe graphic (in the lowest priority tier, but the only thing we list).
_PICKAXE_GFX = next(iter(bv._TOOL_PRIORITIES[2][1]))


def test_in_stock_gate():
    assert _in_stock(_item(0x1, _PICKAXE_GFX, 11, 5)) is True
    assert _in_stock(_item(0x2, _PICKAXE_GFX, 11, 1)) is True
    # 0 stock — a guaranteed no-delivery, must be rejected.
    assert _in_stock(_item(0x3, _PICKAXE_GFX, 11, 0)) is False
    # Defensive: a listing with no amount attribute at all is treated as empty.
    assert _in_stock(SimpleNamespace(serial=0x4, graphic=_PICKAXE_GFX, price=11)) is False


def _pick(buy_list, needed_graphics, active_bl, budget):
    """Replicate the priority-loop selection in BuyFromVendor.execute.

    Kept in lock-step with the real loops (same price + stock gates) so the
    test fails the moment the stock gate is dropped from either loop.
    """
    target_item = None
    for graphics_set in needed_graphics:
        for item in buy_list:
            if item.serial in active_bl:
                continue
            if not _in_stock(item):
                continue
            if item.graphic in graphics_set and 0 < item.price <= budget:
                target_item = item
                break
        if target_item:
            break
    return target_item


def test_selection_skips_zero_stock_picks_next_in_stock():
    """A 0-stock pickaxe is jumped over for the in-stock one behind it."""
    out_of_stock = _item(0xA, _PICKAXE_GFX, 11, 0)
    in_stock = _item(0xB, _PICKAXE_GFX, 11, 4)
    buy_list = [out_of_stock, in_stock]
    needed = [grp for _name, grp in bv._TOOL_PRIORITIES if _PICKAXE_GFX in grp]

    chosen = _pick(buy_list, needed, active_bl=set(), budget=100)
    assert chosen is in_stock, "must skip the 0-stock listing"


def test_selection_returns_none_when_only_stock_is_empty():
    """With every affordable tool out of stock, selection yields nothing —
    so execute falls through to MISSING_RESOURCE WITHOUT firing a doomed buy
    (and thus without arming the 10-minute buy-disable cooldown)."""
    buy_list = [
        _item(0xA, _PICKAXE_GFX, 11, 0),
        _item(0xB, _PICKAXE_GFX, 11, 0),
    ]
    needed = [grp for _name, grp in bv._TOOL_PRIORITIES if _PICKAXE_GFX in grp]

    chosen = _pick(buy_list, needed, active_bl=set(), budget=100)
    assert chosen is None


def test_source_loops_keep_the_stock_gate():
    """Source-shape guard: BOTH selection loops in execute must skip listings
    that fail the in-stock gate, not just gate on price."""
    import inspect

    src = inspect.getsource(bv.BuyFromVendor.execute)
    # The gate appears in both the priority loop and the any-tool fallback.
    assert src.count("if not _in_stock(item):") >= 2
