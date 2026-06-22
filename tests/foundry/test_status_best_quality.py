"""status.py's headline ``best`` must report the variance-aware trust signal
(``_selection_quality = min(fitness, reliability)``), not the raw fitness mean.

Companion to test_status_lineage_quality_order: that test pinned the LINEAGE
listing to the trust signal, but the most prominent operator-facing number --
the headline ``best`` -- was left on ``max(g.fitness)`` (the raw mean). A lucky
high-variance elite (per_seed [400, 0] -> mean 200 but reliability 0) then
advertised itself as ``best 200.000`` in the headline even though it sorts LAST
in the lineage body and every other consumer (select.choose_parent, reeval
--elites, observe.history) ranks it last. This is the same display-vs-trust
inconsistency 985ff44 closed in observe.history.

These tests do NOT touch selection weighting, the kernel promotion rule, or the
held-out / min(fitness, reliability) semantics -- only what the status headline
DISPLAYS.
"""
from __future__ import annotations

import re

from foundry import status
from foundry.kernel.archive import Archive, Genome
from foundry.select import _selection_quality


def _g(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": list(per_seed),
        },
    )


def _headline_best(rendered: str) -> float:
    line = rendered.splitlines()[0]
    m = re.search(r"best\s+([-\d.]+)", line)
    assert m, line
    return float(m.group(1))


def test_headline_best_is_the_trust_signal_not_the_high_variance_mean(tmp_path):
    arc = Archive(tmp_path)
    # Steady: mean 50, pstdev 0 -> reliability 50 -> quality 50.
    steady = _g("g_00001", ("COMBAT", 0), [50.0, 50.0])
    # Lucky/volatile: mean 200 but pstdev 200 -> reliability 0 -> quality 0.
    lucky = _g("g_00002", ("MAGIC", 0), [400.0, 0.0])
    arc.add(steady)
    arc.add(lucky)

    # The trust signal ranks the steady elite top (quality 50), and the volatile
    # mean-200 elite has quality 0 -- so the headline best must be 50, NOT 200.
    assert _selection_quality(steady) == 50.0
    assert _selection_quality(lucky) == 0.0
    assert _headline_best(status.render(arc)) == 50.0


def test_headline_best_matches_single_seed_fitness(tmp_path):
    # Legacy/single-seed genomes have no spread, so reliability falls back to the
    # point fitness and _selection_quality == fitness. The headline best must then
    # equal the top raw fitness -- the fix only changes genuinely volatile elites.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), [30.0]))
    arc.add(_g("g_00002", ("COMBAT", 1), [10.0]))
    assert _headline_best(status.render(arc)) == 30.0


def test_empty_grid_headline_best_is_zero(tmp_path):
    arc = Archive(tmp_path)
    assert _headline_best(status.render(arc)) == 0.0
