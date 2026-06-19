"""Tests for buy-outcome classification.

REGRESSION: a vendor buy that DEBITS gold but delivers NO item must be
treated as a failure (blacklist + 10-min cooldown), never reported as a
success. ServUO's BaseVendor.OnBuyItems can charge the pouch yet clamp
delivery to zero when the stock snapshot we planned against goes stale;
the old logic (failure only when ``gold_spent == 0``) let that money-loss
slip through as success and re-picked the same item next tick.
"""

from anima.procedures.buy_from_vendor import _is_buy_failure


def test_item_delivered_is_success():
    """Tool arrived (pouch-paid) -> not a failure."""
    assert _is_buy_failure(item_gained=True, gold_spent=33) is False


def test_item_delivered_bank_paid_is_success():
    """Tool arrived, no pouch gold moved (bank paid) -> not a failure."""
    assert _is_buy_failure(item_gained=True, gold_spent=0) is False


def test_nothing_happened_is_failure():
    """No item, no gold moved -> failure (the always-detected case)."""
    assert _is_buy_failure(item_gained=False, gold_spent=0) is True


def test_gold_debited_no_item_is_failure():
    """THE BUG: pouch charged but no tool delivered -> must be a failure.

    The pre-fix condition ``not item_gained and gold_spent == 0`` returned
    False here, so the procedure reported success and lost the gold.
    """
    assert _is_buy_failure(item_gained=False, gold_spent=50) is True
