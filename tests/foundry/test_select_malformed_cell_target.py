"""suggest_target_cell must stay TOTAL when an elite has a malformed/empty cell.

A genome whose eval produced no descriptor records ``eval["cell"] = []``
(orchestrator ``_genome_from``: ``"cell": list(d.cell) if d else []``), so its
``Genome.cell`` is the empty tuple ``()``. ``choose_parent``/``_neighbors`` were
hardened against this degenerate grid (commit 094f6ac;
test_select_malformed_cell_parent), but the symmetric TARGET-suggestion path was
not: ``_row_best_quality`` and the ``row_filled`` loop in ``suggest_target_cell``
indexed ``g.cell[0]`` over the raw, unfiltered elite set and raised
``IndexError: tuple index out of range``.

That call sits at the very top of every orchestrator cycle
(``select.suggest_target_cell(arc, seed=i)``), inside the archive lock, so a
single empty-cell elite crashed it and — because the grid stays poisoned —
wedged every remaining cycle of the run, silently producing zero genomes. These
tests pin the total-fallback contract for the target side too.
"""
from foundry.kernel.archive import Genome, cell_to_str
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


class _FakeArchive:
    """Minimal Archive double exposing elites()/get_elite()/grid + empty cells."""

    def __init__(self, elites: list[Genome]) -> None:
        self._elites = list(elites)
        self.grid = {cell_to_str(tuple(g.cell)): g.id for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._elites)

    def get_elite(self, cell: tuple):
        gid = self.grid.get(cell_to_str(tuple(cell)))
        return next((g for g in self._elites if g.id == gid), None)


def test_row_best_quality_skips_empty_cell_elite():
    # The empty-cell elite has no profession row; it is omitted, not crashed on.
    failed = _mk("g_fail", (), [50.0, 50.0, 50.0])
    real = _mk("g_real", ("GATHERING", 0), [10.0, 11.0, 12.0])
    best = _row_best_quality([failed, real])
    assert "GATHERING" in best
    assert "" not in best                 # no phantom empty-profession row
    # the row best is the real elite's variance-aware quality (not the empty one)
    assert best["GATHERING"] == _selection_quality(real)
    assert best.keys() == {"GATHERING"}   # the empty-cell elite contributes nothing


def test_suggest_target_stays_total_with_sole_empty_cell_elite():
    # The ONLY grid occupant has an empty cell (a failed-descriptor eval). Before
    # the fix this raised IndexError out of _row_best_quality / the row_filled
    # loop and killed the cycle (and so every subsequent cycle).
    failed = _mk("g_fail", (), [5.0, 6.0, 7.0])
    arc = _FakeArchive([failed])
    # Grid is otherwise empty → there ARE empty cells to explore; must return one.
    target = suggest_target_cell(arc, seed=0)
    assert target is not None
    assert target[0] != "NONE"            # never aims at the NONE fallback row


def test_suggest_target_ignores_empty_cell_elite_alongside_real_ones():
    # A real-profession elite plus the degenerate one: selection still works and
    # never crashes regardless of the random seed.
    failed = _mk("g_fail", (), [99.0, 99.0, 99.0])
    real = _mk("g_real", ("GATHERING", 0), [10.0, 11.0, 12.0])
    arc = _FakeArchive([failed, real])
    for seed in range(50):
        target = suggest_target_cell(arc, seed=seed)
        assert target is not None
        assert target[0] != "NONE"
