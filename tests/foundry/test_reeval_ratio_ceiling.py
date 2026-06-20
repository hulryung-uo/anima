"""A small-but-above-1e-6 recorded fitness must not produce a giant FINITE
replication ratio that swamps the cross-genome demotion median.

Regression: ``_replication_ratio`` only treated recorded fitness AT OR BELOW
``_NEGLIGIBLE_FITNESS`` (1e-6) as scale-free (→ inf over-replication). But
``fitness = gate * inner`` spans a continuum of small scores; a realistic
low-viability-gate genome archives e.g. 5e-4 — above the 1e-6 guard yet still
with no usable scale. A held-out recovery of a real score then yields a finite
ratio in the hundreds of thousands that survives the inf-filter in
``_summarize_ratios`` and dominates the median demotion verdict — the exact
failure the near-zero guard exists to prevent, leaking through just above its
arbitrary threshold. Ratios past a sane ceiling are now reclassified as the
over-replication they are.
"""
from __future__ import annotations

import statistics

from foundry import reeval

INF = float("inf")


def test_small_recorded_big_recovery_is_over_replication_not_giant_finite():
    # recorded 5e-4 is ABOVE _NEGLIGIBLE_FITNESS (1e-6) — the old guard misses it.
    r = reeval._replication_ratio(5e-4, 120.0)
    assert r == INF, f"expected inf over-replication, got {r}"
    # Same for a tiny-but-above-threshold negative recorded with a real recovery.
    assert reeval._replication_ratio(-5e-4, 120.0) == INF


def test_legitimate_verdict_bands_are_unchanged():
    # Everything inside a sane band keeps its exact finite value — the verdict
    # thresholds (≤0.5 demote, 0.5-0.8 weak, ≥0.8 replicates) must not move.
    assert reeval._replication_ratio(10.0, 8.0) == 0.8
    assert reeval._replication_ratio(10.0, 5.0) == 0.5
    assert reeval._replication_ratio(10.0, 12.0) == 1.2   # small over-replication, FINITE
    assert reeval._replication_ratio(-1.0, -0.5) > 0.8
    # A modest over-recovery (≤ ceiling) stays finite, not collapsed to inf.
    assert reeval._replication_ratio(10.0, 100.0) == 10.0
    assert reeval._replication_ratio(10.0, 100.0) != INF
    # Just past the ceiling → over-replication.
    assert reeval._replication_ratio(10.0, 111.0) == INF


def test_near_zero_guard_still_holds():
    # The original scale-free behaviour is preserved unchanged.
    assert reeval._replication_ratio(1e-9, 50.0) == INF
    assert reeval._replication_ratio(0.0, 5.0) == INF
    assert reeval._replication_ratio(0.0, 0.0) == 1.0
    assert reeval._replication_ratio(1e-9, -3.0) == 1.0


def test_small_recorded_over_replication_excluded_from_median():
    # Two clear shortfalls + one small-recorded genome that recovered big. The
    # median demotion-evidence number must summarise the two shortfalls only;
    # the over-replication is set aside and COUNTED, not folded in as a giant
    # finite value that drags the median upward.
    ratios = [
        reeval._replication_ratio(10.0, 2.0),    # 0.2 — clear shortfall
        reeval._replication_ratio(10.0, 4.0),    # 0.4 — clear shortfall
        reeval._replication_ratio(5e-4, 120.0),  # small recorded, big recovery → inf
    ]
    # Pre-fix the third entry was ~2.4e5 (finite): median of [0.2, 0.4, 2.4e5]
    # was 0.4 and "over-replicated" read 0 — the demotion signal silently shifted.
    assert statistics.median(sorted(ratios)) != ratios[2]  # sanity: it's the inf one
    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    assert "inf" not in summary.lower()
    assert "0.30" in summary               # median over the two finite shortfalls
    assert "over 2 genomes" in summary
    assert "1 over-replicated" in summary
