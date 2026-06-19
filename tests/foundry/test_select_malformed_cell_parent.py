"""choose_parent must stay TOTAL even when an elite has a malformed/empty cell.

A genome whose eval produced no descriptor records ``eval["cell"] = []``
(orchestrator ``_genome_from``: ``"cell": list(d.cell) if d else []``), so its
``Genome.cell`` is the empty tuple ``()``. choose_parent's docstring and the
sibling test ``test_falls_back_to_none_only_when_grid_is_all_none`` both promise
it returns a parent rather than crashing on a degenerate grid. But the
``targetable or elites`` fallback could route an empty-cell elite into
``_frontier_potential`` -> ``_neighbors(())``, where ``prof, soc = ()`` raised
``ValueError: not enough values to unpack`` and aborted the cycle. These tests
pin the total-fallback contract for the empty/short-cell case too.
"""
from foundry.kernel.archive import Genome, cell_to_str
from foundry.select import _frontier_potential, _neighbors, choose_parent


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
    def __init__(self, elites: list[Genome]) -> None:
        self._elites = list(elites)
        self.grid = {cell_to_str(tuple(g.cell)): g.id for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._elites)


def test_neighbors_tolerates_malformed_cell():
    # No (prof, soc) pair -> no ordinal neighbour, and never a crash.
    assert _neighbors(()) == []
    assert _neighbors(("GATHERING",)) == []
    # A well-formed cell still gets its sociability neighbours.
    assert _neighbors(("GATHERING", 0)) == [("GATHERING", 1)]


def test_frontier_potential_of_malformed_cell_is_zero():
    # A frontier count over a malformed cell is well-defined (zero) rather than
    # raising — it has no targetable empty neighbour by construction.
    assert _frontier_potential((), set()) == 0


def test_choose_parent_stays_total_with_sole_empty_cell_elite():
    # The ONLY grid occupant has an empty cell (a failed-descriptor eval). Before
    # the fix this raised ValueError out of _neighbors and killed the cycle.
    failed = _mk("g_fail", (), [5.0, 6.0, 7.0])
    arc = _FakeArchive([failed])

    pick = choose_parent(arc, seed=0)
    assert pick is not None
    assert pick.id == "g_fail"


def test_choose_parent_prefers_real_elite_over_empty_cell_elite():
    # With a real-profession elite present, the empty-cell elite is filtered out
    # of the targetable pool entirely (its cell is falsy), so a real parent wins
    # — and selection never crashes regardless of the random seed.
    failed = _mk("g_fail", (), [50.0, 50.0, 50.0])
    real = _mk("g_real", ("COMBAT", 1), [10.0, 11.0, 12.0])
    arc = _FakeArchive([failed, real])

    for seed in range(100):
        pick = choose_parent(arc, seed=seed)
        assert pick is not None
        assert pick.id == "g_real", f"seed {seed} picked the empty-cell elite"
