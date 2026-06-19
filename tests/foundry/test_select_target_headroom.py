"""FULL-GRID headroom targeting must not be poisoned by a phantom 0.0 row ceiling.

suggest_target_cell() aims an EXPLORE/IMPROVE mutation at the cell with the
largest gap to its row's best elite. _selection_quality can legitimately be < 0
(a low-fitness, high-variance elite whose reliability = mean − λ·pstdev dips
below zero). The old running max seeded at 0.0 reported a row-best of 0.0 for any
row whose elites are ALL negative — a value no genome reached — so the row-best
cell (true headroom 0 against itself) was handed a positive, inflated weight
instead of the floor. _row_best_quality reports the real (possibly negative) max.
"""
from types import SimpleNamespace

from foundry.kernel.archive import Genome
from foundry.select import _row_best_quality, _selection_quality, suggest_target_cell


def _mk(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": per_seed,
        },
    )


def test_row_best_quality_honors_all_negative_row():
    # Both COMBAT elites have negative selection-quality (high-variance, low
    # fitness). The true row best is the LESS-negative one — not a phantom 0.0.
    g0 = _mk("g_a", ("COMBAT", 0), [0.0, 0.0, 3.0])   # quality ≈ -0.414
    g1 = _mk("g_b", ("COMBAT", 1), [0.0, 0.0, 1.5])   # quality ≈ -0.207 (best)
    assert _selection_quality(g0) < 0.0
    assert _selection_quality(g1) < 0.0
    rb = _row_best_quality([g0, g1])
    # The real max, NOT 0.0.
    assert rb["COMBAT"] == max(_selection_quality(g0), _selection_quality(g1))
    assert rb["COMBAT"] < 0.0


class _FakeArchive:
    """Minimal Archive double exposing elites()/get_elite() over a fixed grid."""

    def __init__(self, elites: list[Genome]) -> None:
        self._by_cell = {tuple(g.cell): g for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._by_cell.values())

    def get_elite(self, cell: tuple) -> Genome | None:
        return self._by_cell.get(tuple(cell))


def _full_grid_weight(archive, row_best, cell) -> float:
    # Mirror the FULL-GRID weight formula in suggest_target_cell.
    return 1.0 + max(
        0.0, row_best[cell[0]] - _selection_quality(archive.get_elite(cell))
    )


def test_full_grid_row_best_cell_gets_floor_weight():
    # A 2-cell COMBAT row, both elites negative-quality, grid otherwise "full"
    # (no empty cells reachable through this double). The cell that IS the row
    # best has zero headroom against itself → its weight must be exactly the
    # 1.0 floor. Under the 0.0-floor bug it was 1.0 + |its negative quality|.
    g0 = _mk("g_a", ("COMBAT", 0), [0.0, 0.0, 3.0])
    g1 = _mk("g_b", ("COMBAT", 1), [0.0, 0.0, 1.5])   # row best
    arc = _FakeArchive([g0, g1])
    rb = _row_best_quality(arc.elites())

    best_cell = ("COMBAT", 1)
    worse_cell = ("COMBAT", 0)
    w_best = _full_grid_weight(arc, rb, best_cell)
    w_worse = _full_grid_weight(arc, rb, worse_cell)

    # Row-best cell: exactly the floor (no headroom against itself).
    assert w_best == 1.0
    # The worse cell carries the real (positive) headroom gap.
    assert w_worse > 1.0
    # And exploration weight is monotonic in headroom: worse cell > best cell.
    assert w_worse > w_best


def test_suggest_target_full_grid_runs_on_negative_row():
    # End-to-end smoke: with empties forced empty (single row fully covered by
    # the active-grid for this profession is enough to exercise the branch on
    # the double), suggest_target_cell returns a valid elite cell and never
    # raises on an all-negative row.
    g0 = _mk("g_a", ("COMBAT", 0), [0.0, 0.0, 3.0])
    g1 = _mk("g_b", ("COMBAT", 1), [0.0, 0.0, 1.5])

    class _FullArchive(_FakeArchive):
        # grid keys cover every active cell so empty_cells() == [] (FULL GRID).
        @property
        def grid(self):
            from foundry.select import all_active_cells
            from foundry.kernel.archive import cell_to_str
            return {cell_to_str(c): "g_x" for c in all_active_cells()}

    arc = _FullArchive([g0, g1])
    out = suggest_target_cell(arc, seed=0)
    assert out in (("COMBAT", 0), ("COMBAT", 1))
