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


def test_near_zero_recorded_is_scale_free_not_an_exploding_ratio():
    # fitness is gate*inner, so a barely-viable genome archives a TINY non-zero
    # total (e.g. 1e-9), not exactly 0.0. Dividing the held-out recovery by that
    # tiny denominator used to yield an astronomically large FINITE ratio
    # (~5e10) that the exact ``recorded == 0.0`` guard never caught. A near-zero
    # recorded must be treated like exact zero: scale-free.
    assert reeval._replication_ratio(1e-9, 50.0) == float("inf")   # was ~5e10
    assert reeval._replication_ratio(-1e-9, 50.0) == float("inf")
    assert reeval._replication_ratio(1e-9, 0.0) == 1.0
    assert reeval._replication_ratio(1e-9, -3.0) == 1.0
    # A genuinely-scaled recorded fitness is UNCHANGED (well above the band).
    assert reeval._replication_ratio(10.0, 8.0) == 0.8
    assert reeval._replication_ratio(-1.0, -0.5) == reeval._replication_ratio(-1.0, -0.5)


def test_near_zero_recorded_does_not_poison_the_cross_genome_median():
    # The headline demotion-evidence number is the median over FINITE ratios
    # (_summarize_ratios drops the inf over-replications). A near-zero-recorded
    # genome that recovered a real positive score must register as an inf
    # over-replication (excluded), NOT as a giant finite ratio that swamps the
    # median of the genomes that actually replicated.
    ratios = [
        reeval._replication_ratio(10.0, 2.0),    # 0.2  — clear shortfall
        reeval._replication_ratio(10.0, 4.0),    # 0.4  — clear shortfall
        reeval._replication_ratio(1e-9, 50.0),   # ~0 recorded, big recovery
    ]
    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    # Median taken over the two FINITE shortfall ratios only → 0.30; the
    # near-zero genome is reported as a separate over-replication count.
    assert "0.30" in summary
    assert "over 2 genomes" in summary
    assert "1 over-replicated" in summary
    # Pre-fix: the near-zero genome produced a ~5e10 FINITE ratio, the median of
    # [0.2, 0.4, 5e10] was 0.4, and "over-replicated" was 0 — the demotion
    # signal silently shifted by a genome the summary should have set aside.


def test_catastrophic_regression_is_floored_finite():
    """A scale-free regression outlier is clamped to a FINITE floor, not -inf.

    A small-magnitude recorded fitness with a large-negative held_out yields a
    huge finite negative ratio (e.g. -1e6) that — unlike the over-replication
    case which becomes inf and is set aside — survives _summarize_ratios'
    finite-filter and single-handedly swamps the even-count cross-genome median.
    Clamp it to a finite floor so it stays in the median as demotion evidence
    (a regression IS real evidence) but cannot dominate it.
    """
    r = reeval._replication_ratio(1e-3, -1000.0)
    assert r == reeval._CATASTROPHIC_REGRESSION_FLOOR
    assert r != float("-inf")          # finite — stays in the median
    assert r < 0.5                     # still reads as "does not replicate"
    # A moderate regression at/above the floor is left exactly as computed:
    # recorded=-1.0, held_out=-1.5 -> ratio = 1 - (-1 - -1.5)/1 = 0.5
    assert reeval._replication_ratio(-1.0, -1.5) == 0.5
    # The over-replication ceiling (the opposite extreme) is unaffected.
    assert reeval._replication_ratio(1e-3, 1000.0) == float("inf")


def test_verdict_over_replication_is_not_labelled_clean_replication():
    """An ``inf`` (over-replication) ratio must NOT read as "replicates".

    ``_summarize_ratios`` already sets ``inf`` aside as a distinct
    "over-replicated (recorded ~0, recovered positive)" category. The per-line
    ``_verdict`` used a bare ``< 0.5`` / ``< 0.8`` / else chain in which ``inf``
    fell straight through to the "replicates" branch — telling the operator the
    archived score reproduced cleanly for exactly the genome whose archived
    number was unreliable (recorded ~0). The verdict must agree with the
    aggregate framing.
    """
    over = {
        "ok": True,
        "ratio": reeval._replication_ratio(1e-9, 50.0),  # → inf
        "cell": ["COMBAT", 1],
        "held_out_cell": ["COMBAT", 1],
    }
    assert over["ratio"] == float("inf")
    verdict = reeval._verdict(over)
    assert "OVER-REPLICAT" in verdict
    assert "replicates" not in verdict  # not the clean-replication label

    # A genuine clean replication is untouched, and a shortfall still demotes —
    # the other branches must keep working.
    clean = {"ok": True, "ratio": 0.95, "cell": ["COMBAT", 1],
             "held_out_cell": ["COMBAT", 1]}
    assert reeval._verdict(clean) == "replicates"
    short = {"ok": True, "ratio": 0.3, "cell": ["COMBAT", 1],
             "held_out_cell": ["COMBAT", 1]}
    assert "DOES NOT REPLICATE" in reeval._verdict(short)
