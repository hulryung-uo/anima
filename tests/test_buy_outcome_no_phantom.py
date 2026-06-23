"""BuyFromNpc must not report a gold-spent / zero-item buy as a success.

Regression: ``anima.skills.trade.vendor.BuyFromNpc.execute`` only special-cased
the all-zero outcome (``items_received == 0 and gold_spent == 0``). When gold
actually LEFT the wallet but none of the needed tools arrived (full pack, a
bounced/over-stock line, or a wrong-graphic delivery), it fell through to the
``success=True`` tail with a positive ``1.0 + gold_spent * 0.01`` reward — a
phantom restock that poisons the reward signal and tells the planner the buy
landed, so it stops retrying. Mirrors the SellToNpc no-gold-change phantom-sell
guard.

Exercises the pure ``_buy_outcome`` helper directly, so no async / sleep is
involved.
"""

from __future__ import annotations

import pytest

from anima.skills.trade.vendor import _buy_outcome


def test_gold_spent_but_no_items_is_failure() -> None:
    # The core regression: 50gp left the wallet, zero needed tools arrived.
    success, reward = _buy_outcome(items_received=0, gold_spent=50)
    assert success is False
    assert reward < 0  # NOT the old phantom +1.0 + 50*0.01


def test_items_received_is_success_with_positive_reward() -> None:
    success, reward = _buy_outcome(items_received=2, gold_spent=120)
    assert success is True
    assert reward == pytest.approx(1.0 + 120 * 0.01)


def test_items_received_with_unknown_gold_still_succeeds() -> None:
    # Tools arrived but the gold delta hadn't settled — still a win.
    success, reward = _buy_outcome(items_received=1, gold_spent=0)
    assert success is True
    assert reward == pytest.approx(1.0)


def test_nothing_changed_is_failure() -> None:
    # All-zero: the no-gold path (caller owns the cooldown/rethink).
    success, reward = _buy_outcome(items_received=0, gold_spent=0)
    assert success is False
    assert reward < 0


@pytest.mark.parametrize(
    "items_received,gold_spent,expected_success",
    [
        (0, 0, False),
        (0, 1, False),
        (0, 999, False),
        (1, 0, True),
        (3, 250, True),
    ],
)
def test_buy_outcome_table(
    items_received: int, gold_spent: int, expected_success: bool,
) -> None:
    success, _reward = _buy_outcome(items_received, gold_spent)
    assert success is expected_success
