"""Tests for bank-balance-cache reconciliation after a vendor buy.

REGRESSION: a bank-paid purchase must decrement the cached bank balance by
``price * delivered`` (the real backpack-count delta), never by the planned
``buy_qty``. ServUO clamps delivery to the vendor's *current* stock, so a
stale 0x74/0x3C stock snapshot can make us plan more than the server hands
over. Charging the cache for the planned quantity over-decrements it and
corrupts the next affordability decision.
"""

import time
from unittest.mock import MagicMock

from anima.procedures.buy_from_vendor import _reconcile_bank_after_buy


def _ctx(bank_amount: int):
    ctx = MagicMock()
    ctx.blackboard = {"bank_balance": {"amount": bank_amount, "ts": time.time()}}
    return ctx


def test_charges_delivered_not_planned():
    """Planned 3 but only 2 delivered -> cache drops by price*2, not price*3."""
    ctx = _ctx(bank_amount=1000)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=2, gold_spent=0, bank_before=1000
    )
    # Over-decrementing (the old bug, price*buy_qty=33) would leave 967.
    assert ctx.blackboard["bank_balance"]["amount"] == 1000 - 11 * 2  # 978


def test_full_delivery_charges_full_amount():
    ctx = _ctx(bank_amount=1000)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=3, gold_spent=0, bank_before=1000
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 1000 - 11 * 3  # 967


def test_pouch_paid_purchase_leaves_cache_untouched():
    """gold_spent>0 means the backpack paid; the bank cache must not move."""
    ctx = _ctx(bank_amount=1000)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=3, gold_spent=33, bank_before=1000
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 1000


def test_nothing_delivered_leaves_cache_untouched():
    """A no-delivery outcome must not decrement the cache at all."""
    ctx = _ctx(bank_amount=1000)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=0, gold_spent=0, bank_before=1000
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 1000


def test_no_cached_balance_is_a_noop():
    """With no bank-balance cache present, reconciliation does nothing."""
    ctx = MagicMock()
    ctx.blackboard = {}
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=2, gold_spent=0, bank_before=500
    )
    assert "bank_balance" not in ctx.blackboard


def test_cache_floors_at_zero():
    """A charge larger than the cached balance floors at 0, never negative."""
    ctx = _ctx(bank_amount=10)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=2, gold_spent=0, bank_before=10
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 0


def test_zero_bank_before_skips_reconcile():
    """bank_before==0 means the bank could not have paid -> leave cache as-is."""
    ctx = _ctx(bank_amount=42)
    _reconcile_bank_after_buy(
        ctx, price=11, delivered=2, gold_spent=0, bank_before=0
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 42
