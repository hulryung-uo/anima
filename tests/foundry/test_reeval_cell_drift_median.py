"""A cell-DRIFTED held-out re-run is not like-for-like replication evidence for
the archived cell, so its ratio must be kept OUT of the cross-genome median.

The replication ratio normalises a held-out score against the fitness recorded
in the genome's archived cell. When the re-run lands in a DIFFERENT behavioral
cell, the two numbers describe different niches; folding that ratio into the
headline median skews this tool's demotion-evidence number the same way a
doubled or ``inf`` entry would (cf. the dedup / inf-filter fixes, and
orchestrator._pool_confirmation, which drops off-cell confirm rounds for the
same reason). The drift is still surfaced per-line by _verdict.
"""
from __future__ import annotations

from foundry import reeval


def _r(*, cell, held_out_cell, ratio, ok=True):
    return {
        "ok": ok,
        "cell": list(cell),
        "held_out_cell": list(held_out_cell),
        "ratio": ratio,
    }


def test_drifted_genome_is_excluded_from_the_median():
    # Two on-cell genomes that clearly fail to replicate, plus one DRIFTED genome
    # whose (high) ratio would pull the median up if it were counted.
    results = [
        _r(cell=("COMBAT", 2), held_out_cell=("COMBAT", 2), ratio=0.2),
        _r(cell=("COMBAT", 2), held_out_cell=("COMBAT", 2), ratio=0.4),
        _r(cell=("COMBAT", 2), held_out_cell=("COMBAT", 1), ratio=2.0),  # drift
    ]
    ratios, n_drift = reeval._comparable_ratios(results)

    assert ratios == [0.2, 0.4]
    assert n_drift == 1
    # The headline median reflects only the like-for-like (failing) replications.
    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    assert "0.30" in summary          # median of [0.2, 0.4]
    assert "over 2 genomes" in summary


def test_on_cell_genomes_are_all_kept():
    results = [
        _r(cell=("MAGIC", 0), held_out_cell=("MAGIC", 0), ratio=0.5),
        _r(cell=("MAGIC", 1), held_out_cell=("MAGIC", 1), ratio=0.9),
    ]
    ratios, n_drift = reeval._comparable_ratios(results)
    assert ratios == [0.5, 0.9]
    assert n_drift == 0


def test_failed_evals_are_not_counted_as_drift():
    # A failed eval has no held_out_cell; it is neither comparable nor a drift.
    results = [
        _r(cell=("GATHERING", 0), held_out_cell=("GATHERING", 0), ratio=0.7),
        {"ok": False, "cell": ["GATHERING", 1], "error": "boom"},
    ]
    ratios, n_drift = reeval._comparable_ratios(results)
    assert ratios == [0.7]
    assert n_drift == 0


def test_is_cell_comparable_predicate():
    assert reeval._is_cell_comparable(
        _r(cell=("COMBAT", 0), held_out_cell=("COMBAT", 0), ratio=1.0))
    assert not reeval._is_cell_comparable(
        _r(cell=("COMBAT", 0), held_out_cell=("COMBAT", 1), ratio=1.0))
    assert not reeval._is_cell_comparable({"ok": False, "cell": ["COMBAT", 0]})


def test_drift_does_not_rescue_a_demotion_signal():
    # Without the filter, the drifted 2.0 straddles the median of
    # [0.2, 0.4, 0.6, 2.0] and lifts the headline above the 0.5 demotion band,
    # masking three genomes that did not replicate. The filter keeps it honest.
    results = [
        _r(cell=("CRAFTING", 0), held_out_cell=("CRAFTING", 0), ratio=0.2),
        _r(cell=("CRAFTING", 1), held_out_cell=("CRAFTING", 1), ratio=0.4),
        _r(cell=("CRAFTING", 2), held_out_cell=("CRAFTING", 2), ratio=0.6),
        _r(cell=("CRAFTING", 0), held_out_cell=("BARD-SOCIAL", 0), ratio=2.0),
    ]
    ratios, n_drift = reeval._comparable_ratios(results)
    assert n_drift == 1
    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    # median of [0.2, 0.4, 0.6] == 0.4 — still inside the "weak/demote" range,
    # not the inflated 0.5 the drifted run would have produced.
    assert "0.40" in summary
    assert "over 3 genomes" in summary
