"""status.py's GRID TABLE cell must show the variance-aware trust signal
(``_selection_quality = min(fitness, reliability)``), not the raw fitness mean.

Companion to test_status_best_quality (headline ``best``) and
test_status_lineage_quality_order (lineage listing): both pinned their figure to
the trust signal, but the spatial GRID TABLE cell was left on ``g.fitness`` (the
raw mean). The file's own invariant says a lucky high-variance elite must not
advertise a number "the headline AND GRID both deny" -- yet a cell still
rendering the mean floated a coin-flip elite (per_seed [400, 0] -> mean 200 but
reliability/quality 0) as a ``200.00`` cell while the headline best (50) and the
lineage (q 0.00) both ranked it last. This pins the table to the same signal so
every face of the report describes one universe.

These tests do NOT touch selection weighting, the kernel promotion rule, or the
held-out / min(fitness, reliability) semantics -- only what the GRID TABLE
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


def _cell_value_for(rendered: str, gid: str) -> float:
    """The numeric value rendered in the grid-table cell that holds ``gid``."""
    for line in rendered.splitlines():
        if gid in line:
            m = re.search(rf"{re.escape(gid)}\s+([-\d.]+)", line)
            assert m, f"no number beside {gid} in: {line!r}"
            return float(m.group(1))
    raise AssertionError(f"{gid} not found in grid:\n{rendered}")


def test_grid_cell_shows_quality_not_high_variance_mean(tmp_path):
    arc = Archive(tmp_path)
    # Lucky/volatile: mean 200 but pstdev 200 -> reliability 0 -> quality 0.
    lucky = _g("g_00002", ("MAGIC", 0), [400.0, 0.0])
    arc.add(lucky)

    assert lucky.fitness == 200.0
    assert _selection_quality(lucky) == 0.0

    rendered = status.render(arc)
    # The grid cell for the volatile elite must read its quality (0.00), NOT the
    # raw mean (200.00) -- otherwise it advertises a number the headline best
    # and the lineage both deny.
    assert _cell_value_for(rendered, "g_00002") == 0.0


def test_grid_cell_matches_single_seed_fitness(tmp_path):
    # Legacy/single-seed genomes have no spread, so reliability falls back to the
    # point fitness and _selection_quality == fitness; the cell is unchanged for
    # non-volatile elites.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("GATHERING", 1), [42.0]))
    assert _cell_value_for(status.render(arc), "g_00001") == 42.0
