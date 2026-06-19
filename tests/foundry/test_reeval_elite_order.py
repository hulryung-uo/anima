"""``reeval --elites`` re-checks the LEAST trustworthy champion FIRST.

The held-out pass is demotion evidence and is routinely time-boxed/interrupted,
so the order it visits champions matters: the lucky/high-variance elites — the
ones most in need of a replication check — must come first, not last. The queue
used to be sorted by raw ``g.fitness`` (descending), which floated exactly those
volatile champions to the bottom. ``_elite_reeval_order`` ranks by the same
variance-aware ``min(fitness, reliability)`` signal select/observe/the kernel
already use, ascending, so the steadiest elites yield their slot.

These tests pin the ordering only; they do not touch the grid or the kernel
promotion rule.
"""
from __future__ import annotations

from foundry.kernel.archive import Genome
from foundry.reeval import _elite_reeval_order
from foundry.select import _selection_quality


def _genome(gid: str, per_seed: list[float], cell: tuple = ("COMBAT", 0)) -> Genome:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return Genome(
        id=gid,
        eval={"fitness": mean, "cell": list(cell), "per_seed_fitness": list(per_seed)},
    )


def test_volatile_champion_is_re_checked_before_a_steadier_higher_one():
    # Steady A: per_seed [120,120] → fitness 120, reliability 120, quality 120.
    # Volatile B: per_seed [200,20] → fitness 110, reliability 20, quality 20.
    # Raw-fitness order would put A (120) ahead of B (110); the trusted
    # variance-aware order must put B (quality 20) FIRST — it is the one whose
    # archived score is most likely not to replicate.
    a = _genome("g_00001", [120.0, 120.0])
    b = _genome("g_00002", [200.0, 20.0])
    assert a.fitness > b.fitness                      # raw order: A before B
    assert _selection_quality(b) < _selection_quality(a)

    ordered = _elite_reeval_order([a, b])
    assert [g.id for g in ordered] == ["g_00002", "g_00001"]


def test_order_is_ascending_in_selection_quality():
    gs = [
        _genome("g_00010", [50.0, 50.0]),     # quality 50
        _genome("g_00011", [300.0, 0.0]),     # fitness 150, reliability 0 → quality 0
        _genome("g_00012", [90.0, 80.0]),     # quality 85 - 5 = 80 ... see below
    ]
    ordered = _elite_reeval_order(gs)
    qualities = [_selection_quality(g) for g in ordered]
    assert qualities == sorted(qualities), "least-trustworthy elite must come first"
    # The lucky high-fitness elite (g_00011, fitness 150) is NOT first by raw
    # fitness, yet it leads the re-eval queue because its lower bound is 0.
    assert ordered[0].id == "g_00011"


def test_genome_id_breaks_ties_deterministically():
    # Two elites with identical per-seed evidence → identical selection quality.
    # The id tiebreak keeps the order stable and reproducible across runs.
    x = _genome("g_00021", [70.0, 70.0])
    y = _genome("g_00020", [70.0, 70.0])
    assert _selection_quality(x) == _selection_quality(y)
    assert [g.id for g in _elite_reeval_order([x, y])] == ["g_00020", "g_00021"]
    assert [g.id for g in _elite_reeval_order([y, x])] == ["g_00020", "g_00021"]


def test_empty_elite_set_is_handled():
    assert _elite_reeval_order([]) == []
