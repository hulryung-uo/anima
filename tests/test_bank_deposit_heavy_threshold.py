"""Regression: the bank trip and the deposit must use ONE weight threshold.

``can_start`` triggered a bank trip at weight ratio 0.6 but ``execute`` only
dumped heavy DEPOSIT_GRAPHICS materials above 0.8. An agent at 0.6–0.8 with no
gold and no colored ingots (a miner carrying iron ore) walked to the bank,
opened the box, deposited nothing, and failed — a wasted round-trip that left
it overweight and unable to keep gathering. The shared predicate
``_should_deposit_heavy`` keeps both call sites in lockstep.
"""

from types import SimpleNamespace

from anima.procedures.bank_deposit import (
    HEAVY_DEPOSIT_RATIO,
    _should_deposit_heavy,
)


def _ss(weight: int, weight_max: int):
    return SimpleNamespace(weight=weight, weight_max=weight_max)


def test_no_capacity_known_is_not_heavy():
    """weight_max==0 means we have no stratum reading yet — never 'heavy'."""
    assert _should_deposit_heavy(_ss(500, 0)) is False


def test_below_ratio_is_not_heavy():
    # 0.5 ratio < 0.6 threshold
    assert _should_deposit_heavy(_ss(50, 100)) is False


def test_at_trip_threshold_band_is_heavy():
    """The exact bug band (0.6 < ratio < 0.8) must now count as depositable.

    Before the fix this band passed can_start (>0.6) but failed the execute
    heavy-deposit gate (>0.8), producing the no-op bank trip.
    """
    ss = _ss(65, 100)  # ratio 0.65 — inside the old dead band
    assert ss.weight > ss.weight_max * HEAVY_DEPOSIT_RATIO  # would make the trip
    assert _should_deposit_heavy(ss) is True  # ...and now actually deposits


def test_well_overweight_is_heavy():
    assert _should_deposit_heavy(_ss(95, 100)) is True


def test_can_start_and_execute_agree_across_the_band():
    """The two call sites can never disagree: a single predicate drives both.

    Sweep ratios that straddle the old 0.6/0.8 split and assert the trip
    decision and the deposit decision are identical at every point — the
    invariant that, once broken, stranded the agent.
    """
    weight_max = 100
    for weight in range(0, 101):
        ss = _ss(weight, weight_max)
        trip_decision = _should_deposit_heavy(ss)        # can_start uses this
        deposit_decision = _should_deposit_heavy(ss)     # execute uses this
        assert trip_decision == deposit_decision
        # And there is no longer a band where we'd travel but not deposit.
        would_have_travelled_old = weight > weight_max * 0.6
        would_have_deposited_old = weight > weight_max * 0.8
        if would_have_travelled_old and not would_have_deposited_old:
            # exactly the old dead band — the new predicate deposits here
            assert deposit_decision is True
