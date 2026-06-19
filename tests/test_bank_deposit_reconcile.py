"""Tests for bank-balance-cache reconciliation after a deposit.

REGRESSION: the gather->sell->bank->restock loop stranded the agent. The
``bank_balance`` cache is decremented by a bank-paid buy but was NEVER
incremented by a deposit, so after banking gold the cache lagged reality by
everything just deposited. With a still-fresh small pre-deposit reading the
planner trusted it (bal_amount < PICKAXE_COST) and disabled buys for 10 min,
leaving the agent tool-less on top of a full bank. A deposit must bump the
cache by the deposited amount — the mirror of the buy-side reconcile.
"""

import time
from unittest.mock import MagicMock

from anima.procedures.bank_deposit import _reconcile_bank_after_deposit


def _ctx(bank_amount: int, *, ts: float | None = None):
    ctx = MagicMock()
    ctx.blackboard = {
        "bank_balance": {"amount": bank_amount, "ts": ts or time.time()}
    }
    return ctx


def test_deposit_increments_cache():
    """Banking 500 onto a cached 30 leaves the cache reading 530."""
    ctx = _ctx(bank_amount=30)
    _reconcile_bank_after_deposit(ctx, deposited=500)
    assert ctx.blackboard["bank_balance"]["amount"] == 530


def test_deposit_refreshes_timestamp():
    """The bump also refreshes ts so the planner trusts the new (true) value."""
    stale = time.time() - 1000  # older than the 600s freshness window
    ctx = _ctx(bank_amount=5, ts=stale)
    _reconcile_bank_after_deposit(ctx, deposited=495)
    assert ctx.blackboard["bank_balance"]["amount"] == 500
    assert ctx.blackboard["bank_balance"]["ts"] > stale


def test_zero_deposit_is_a_noop():
    """Depositing nothing (only items, no gold) leaves the cache unchanged."""
    ctx = _ctx(bank_amount=42)
    before_ts = ctx.blackboard["bank_balance"]["ts"]
    _reconcile_bank_after_deposit(ctx, deposited=0)
    assert ctx.blackboard["bank_balance"]["amount"] == 42
    assert ctx.blackboard["bank_balance"]["ts"] == before_ts


def test_no_cached_balance_is_a_noop():
    """With no prior reading we don't fabricate one (could understate reality)."""
    ctx = MagicMock()
    ctx.blackboard = {}
    _reconcile_bank_after_deposit(ctx, deposited=500)
    assert "bank_balance" not in ctx.blackboard


def test_deposit_then_planner_would_allow_restock():
    """End-to-end of the bug: a stale-small reading + deposit no longer
    looks 'broke' to the restock gate.

    Reproduces the planner Priority 4c check: a FRESH cache below PICKAXE_COST
    disables buys. Before the fix the post-deposit cache still read 5gp and the
    gate fired; after the fix it reads 505gp and the gate passes.
    """
    PICKAXE_COST = 11
    ctx = _ctx(bank_amount=5)  # last balance read before the sell/deposit leg
    _reconcile_bank_after_deposit(ctx, deposited=500)
    bal_amount = ctx.blackboard["bank_balance"]["amount"]
    # The planner would now see plenty of bank gold, not a broke account.
    assert bal_amount >= PICKAXE_COST


def test_negative_cached_amount_floored_before_add():
    """A corrupt negative cached value is floored at 0 before adding the deposit."""
    ctx = _ctx(bank_amount=-50)
    _reconcile_bank_after_deposit(ctx, deposited=100)
    assert ctx.blackboard["bank_balance"]["amount"] == 100
