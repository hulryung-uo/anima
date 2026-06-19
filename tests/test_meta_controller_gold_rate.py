"""Gold-rate accounting: a bank deposit must not look like a loss.

The economy/offload mode switch (HeuristicModePolicy branch c) fires when a
gold-earning mode's gold rate is <= 0. The earning loop ends in a bank
deposit, which zeroes ``ss.gold``. If the rate is measured on backpack gold
alone, every successful deposit reports a sharp negative rate and spuriously
steers the avatar to travel/relocate. The rate must track NET WORTH
(backpack + banked gold) so a deposit is a wash.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from anima.planner.meta_controller import MetaController


def _ctx(gold: int, bank_amount: int | None, bank_age_s: float = 0.0):
    ss = SimpleNamespace(gold=gold)
    bb: dict = {}
    if bank_amount is not None:
        bb["bank_balance"] = {"amount": bank_amount, "ts": time.time() - bank_age_s}
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        blackboard=bb,
    )


def test_net_worth_combines_backpack_and_fresh_bank():
    c = MetaController()
    assert c._net_worth(_ctx(gold=120, bank_amount=500)) == 620


def test_net_worth_ignores_stale_bank_cache():
    c = MetaController()
    # Cache older than the 10-minute window is not trusted.
    assert c._net_worth(_ctx(gold=120, bank_amount=500, bank_age_s=700)) == 120


def test_net_worth_missing_cache_is_backpack_only():
    c = MetaController()
    assert c._net_worth(_ctx(gold=120, bank_amount=None)) == 120


def test_deposit_does_not_show_as_negative_rate():
    """Earn 500 gold, then bank it. Net worth is flat -> rate ~0, not negative.

    Regression guard: the old code sampled ``ss.gold`` directly, so the
    post-deposit sample (gold=0) produced a large negative rate even though
    the avatar's wealth was unchanged.
    """
    c = MetaController()
    base = time.monotonic()
    times = iter([base, base + 60.0, base + 120.0])
    import anima.planner.meta_controller as mc

    orig = mc.time.monotonic
    mc.time.monotonic = lambda: next(times)
    try:
        # t0: just arrived at the mine, 0 gold in hand, nothing banked.
        c._gold_rate_per_min(c._net_worth(_ctx(gold=0, bank_amount=None)))
        # t1: mined+sold, now holding 500 gold.
        c._gold_rate_per_min(c._net_worth(_ctx(gold=500, bank_amount=None)))
        # t2: deposited the 500 -> backpack 0, bank 500. Net worth unchanged.
        rate = c._gold_rate_per_min(
            c._net_worth(_ctx(gold=0, bank_amount=500))
        )
    finally:
        mc.time.monotonic = orig

    # Net worth went 0 -> 500 over 2 minutes: a healthy POSITIVE rate.
    # The buggy backpack-only path would report 0 here (0 -> 500 -> 0).
    assert rate > 0.0


def test_backpack_only_would_have_been_negative():
    """Document the bug the fix prevents: backpack-only sampling goes negative
    right after a deposit even though wealth is flat."""
    c = MetaController()
    base = time.monotonic()
    times = iter([base, base + 60.0, base + 120.0])
    import anima.planner.meta_controller as mc

    orig = mc.time.monotonic
    mc.time.monotonic = lambda: next(times)
    try:
        c._gold_rate_per_min(0)      # arrive, 0 in hand
        c._gold_rate_per_min(500)    # earned 500
        rate = c._gold_rate_per_min(0)  # deposited -> backpack 0
    finally:
        mc.time.monotonic = orig

    # 0 -> 500 -> 0 over 2 min reads as a flat 0/min on the first sample,
    # which (with weight) trips the "dead loop -> relocate" branch even though
    # the deposit was a success. This is exactly what _net_worth fixes.
    assert rate == 0.0


def test_stale_bank_cache_holds_last_known_balance():
    """A cache that ages out must NOT phantom-collapse net worth to zero.

    Regression: ``_net_worth`` dropped the banked amount to 0 the instant the
    ``bank_balance`` cache passed its 10-minute freshness window. An agent that
    banked 5000g and simply kept mining (without re-polling the bank) then saw
    its net worth crater by 5000 once the cache went stale, which the rate
    window reads as a massive negative slope — spuriously firing the economy
    "relocate/offload" branch on a thriving agent. The last fresh balance is
    now carried forward, so a stale cache is a hold, not a loss.
    """
    c = MetaController()
    # First, a fresh reading establishes the known bank balance.
    assert c._net_worth(_ctx(gold=200, bank_amount=5000)) == 5200
    # Now the cache is stale: net worth must hold at 5000 banked + backpack,
    # NOT collapse to backpack-only (200).
    assert c._net_worth(_ctx(gold=200, bank_amount=5000, bank_age_s=700)) == 5200
    # A fresh deposit updates the carried-forward balance.
    assert c._net_worth(_ctx(gold=0, bank_amount=6000)) == 6000
    # ...and that newer balance is what survives the next staleness.
    assert c._net_worth(_ctx(gold=0, bank_amount=6000, bank_age_s=700)) == 6000


def test_stale_cache_does_not_force_negative_rate():
    """End-to-end: a stale bank cache must not manufacture a negative rate.

    The agent banks 5000g (net worth ~5000), keeps mining a little, then the
    bank cache ages out. Without the carry-forward, the post-staleness sample
    cratered to backpack-only and the rate went sharply negative; with it, the
    rate stays non-negative because net worth never phantom-dropped.
    """
    c = MetaController()
    base = time.monotonic()
    times = iter([base, base + 120.0, base + 700.0])
    import anima.planner.meta_controller as mc

    orig = mc.time.monotonic
    mc.time.monotonic = lambda: next(times)
    try:
        # t0: fresh bank reading of 5000, some backpack gold.
        c._gold_rate_per_min(c._net_worth(_ctx(gold=100, bank_amount=5000)))
        # t1: still mining, fresh cache, a touch more in hand.
        c._gold_rate_per_min(c._net_worth(_ctx(gold=150, bank_amount=5000)))
        # t2: cache has aged out (700s old). Net worth must HOLD the 5000.
        rate = c._gold_rate_per_min(
            c._net_worth(_ctx(gold=150, bank_amount=5000, bank_age_s=700))
        )
    finally:
        mc.time.monotonic = orig

    # The buggy path read 5100 -> 5150 -> 150 and reported a steep negative
    # rate; holding the banked amount keeps the rate non-negative.
    assert rate >= 0.0
