"""The held-out replication ratio must stay monotone in held-out fitness even
when the archived fitness is negative.

Regression: reeval computed ``ratio = held_out / recorded``. fitness can be
negative (it carries a net-worth term off gold_delta, so a gold-bleeding combat
loop archives a negative total). With a negative denominator the plain ratio
INVERTS: a held-out run that got worse reads as ratio > 1 ("replicates") and one
that improved reads as ratio < 1 ("demote") — backwards for a demotion-evidence
tool. The fix is a sign-aware shortfall ratio that still reduces to held/recorded
for positive recorded fitness.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Genome


def test_positive_recorded_matches_plain_ratio():
    # Unchanged behaviour for the common (positive fitness) case.
    assert reeval._replication_ratio(10.0, 8.0) == 8.0 / 10.0
    assert reeval._replication_ratio(10.0, 5.0) == 0.5
    assert reeval._replication_ratio(10.0, 12.0) == 1.2


def test_negative_recorded_is_monotone_not_inverted():
    # recorded = -1.0. A held-out that got MUCH worse (-4.0) must score LOW
    # (demotion territory); one that IMPROVED (-0.5) must score HIGH.
    worse = reeval._replication_ratio(-1.0, -4.0)
    same = reeval._replication_ratio(-1.0, -1.0)
    better = reeval._replication_ratio(-1.0, -0.5)
    assert worse < same < better
    assert same == 1.0
    assert worse < 0.5   # flagged "DOES NOT REPLICATE"
    assert better > 0.8  # "replicates"
    # The OLD plain ratio would have inverted these:
    assert (-4.0 / -1.0) > 1.0   # worse read as over-replication (the bug)
    assert (-0.5 / -1.0) < 1.0   # improvement read as a shortfall (the bug)


class _Res:
    ok = True
    score = -4.0          # held-out got worse than the (negative) recorded
    per_seed_fitness = [-4.0]
    cell = ("COMBAT", 1)


def test_verdict_flags_negative_regression_for_demotion(monkeypatch):
    monkeypatch.setattr(reeval, "_prepare_worktree", lambda slot, ref: reeval.__file__)
    monkeypatch.setattr(reeval, "run_eval_multi", lambda cfg, **_k: _Res())

    g = Genome(id="g_00099", code_ref="deadbeef",
               config={"persona": "adventurer", "fixed_start": "warrior"},
               eval={"fitness": -1.0, "cell": ["COMBAT", 1],
                     "per_seed_fitness": [-1.0]})

    r = reeval.reeval_genome(arc=None, g=g, seeds=1, window_s=60)
    assert r["ok"]
    # held-out (-4.0) is a regression vs recorded (-1.0): must read as demotion,
    # NOT as the spurious ratio 4.0 the plain quotient produced.
    assert r["ratio"] < 0.5
    assert "DOES NOT REPLICATE" in reeval._verdict(r)


def test_zero_recorded_does_not_divide_by_zero():
    # No scale to normalise against: a ~0 recovery replicates, a real gain
    # over-replicates, and nothing raises.
    assert reeval._replication_ratio(0.0, 0.0) == 1.0
    assert reeval._replication_ratio(0.0, -1.0) == 1.0
    assert reeval._replication_ratio(0.0, 5.0) == float("inf")
