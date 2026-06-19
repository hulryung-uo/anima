"""The cross-genome replication SUMMARY must not be poisoned by the documented
``inf`` over-replication sentinel.

Regression: ``_main`` printed ``statistics.median(ratios)`` directly. A genome
whose recorded fitness was ~0 but recovered a positive held-out score scores
``float("inf")`` (intended at the per-genome level — see ``_replication_ratio``
and test_reeval_ratio.py). But a lone ``inf`` at/straddling the median position
makes ``statistics.median`` return ``inf``, so the median — this tool's headline
demotion-evidence number — reads ``inf`` even when most genomes clearly FAILED to
replicate. The summary must stay a finite, interpretable number.
"""
from __future__ import annotations

import statistics

from foundry import reeval

INF = float("inf")


def test_lone_over_replication_does_not_poison_the_median():
    # Three clear demotions + one over-replication. The raw median is inf
    # (straddles the two middle values); the summary must reflect the demotions.
    ratios = [0.2, 0.4, INF, INF]
    assert statistics.median(ratios) == INF  # the bug, in the raw form

    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    assert "inf" not in summary.lower()
    # median of the finite ratios [0.2, 0.4] == 0.3
    assert "0.30" in summary
    # the over-replications are still surfaced, not silently dropped
    assert "2 over-replicated" in summary


def test_finite_ratios_summarize_to_their_plain_median():
    summary = reeval._summarize_ratios([0.5, 0.9, 0.95])
    assert summary is not None
    assert "0.90" in summary          # median of the three
    assert "over 3 genomes" in summary
    assert "over-replicated" not in summary  # none to report


def test_all_over_replications_reported_without_inf():
    summary = reeval._summarize_ratios([INF, INF, INF])
    assert summary is not None
    assert "inf" not in summary.lower()
    assert "all 3 genomes" in summary


def test_single_data_point_has_no_summary():
    # Mirrors the old `len(ratios) > 1` guard: one genome is not a distribution.
    assert reeval._summarize_ratios([0.7]) is None
    assert reeval._summarize_ratios([]) is None


def test_summary_matches_old_behaviour_for_all_finite():
    # For the common all-finite case the headline number is unchanged: it is
    # still the median over every genome.
    ratios = [0.1, 0.55, 0.8, 1.2]
    summary = reeval._summarize_ratios(ratios)
    assert summary is not None
    assert f"{statistics.median(ratios):.2f}" in summary
    assert "over 4 genomes" in summary
