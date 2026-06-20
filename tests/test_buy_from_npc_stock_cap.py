"""The BuyFromNpc skill must bound its buy quantity by the vendor's stock.

Regression: ``anima.skills.trade.vendor.BuyFromNpc.execute`` planned the full
missing-tool quantity (e.g. 3 pickaxes) without capping it to the vendor's
displayed stock (``VendorBuyItem.amount``). ServUO clamps the *delivered*
amount server-side, but the client still over-states ``total_cost`` / the
buy detail, and some shards reject an over-stock line outright (sinking the
whole order). The procedures path already caps on stock
(``buy_from_vendor._buy_quantity``); this guards the skills path.

Exercises the pure ``_bounded_buy_qty`` helper directly, so no async / sleep
is involved.
"""

from __future__ import annotations

import pytest

from anima.skills.trade.vendor import _bounded_buy_qty


def test_caps_to_available_stock_when_want_exceeds_it() -> None:
    # Want 3 pickaxes but the vendor only shows 1 -> ask for 1.
    assert _bounded_buy_qty(3, 1) == 1


def test_passes_through_when_stock_covers_want() -> None:
    assert _bounded_buy_qty(3, 20) == 3


def test_exact_stock_is_not_reduced() -> None:
    assert _bounded_buy_qty(3, 3) == 3


@pytest.mark.parametrize("available", [0, -1, -5])
def test_no_stock_yields_zero(available: int) -> None:
    # Empty shelf for this item -> nothing buyable (caller skips it).
    assert _bounded_buy_qty(3, available) == 0


def test_never_negative_even_with_odd_want() -> None:
    assert _bounded_buy_qty(-2, 5) == 0


def test_total_cost_is_computed_from_the_bounded_quantity() -> None:
    """Cost must reflect what the vendor can actually deliver, not the want.

    Reproduces the over-statement the fix removes: with want=3, price=11 and
    only 1 in stock, the planned cost is 11 (1*11), not 33 (3*11).
    """
    want, price, available = 3, 11, 1
    qty = _bounded_buy_qty(want, available)
    assert qty == 1
    assert qty * price == 11
    # The pre-fix unbounded path would have charged the full want.
    assert qty * price != want * price


def test_multiple_items_each_bounded_independently() -> None:
    # (want, available) -> expected planned qty
    cases = [(3, 1), (5, 20), (2, 0), (4, 4)]
    expected = [1, 5, 0, 4]
    assert [_bounded_buy_qty(w, a) for w, a in cases] == expected
