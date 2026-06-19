"""choose_parent must never select a NONE-fallback-row elite as a parent.

uoconst.NONE is the descriptor's "gained no livelihood skill" fallback — a
failed/degenerate agent (wandered or died, banked a high-variance score), not a
behavioral niche. The selection policy already excludes NONE on every TARGET
path (empty_cells / suggest_target_cell / _frontier_potential). But choose_parent
(the frontier-biased PARENT picker, and the no-row-match fallback of
choose_parent_for_target) did not filter the parent pool — so a NONE elite with
a positive selection-quality could still be drawn and mutated FROM, seeding a
lineage on broken/wandering code. These tests pin the parent-side exclusion,
with a total fallback when the grid holds nothing but NONE.
"""
from foundry.kernel import uoconst
from foundry.kernel.archive import Genome
from foundry.select import choose_parent


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
    """Minimal Archive double exposing elites()/grid over a fixed elite set."""

    def __init__(self, elites: list[Genome]) -> None:
        from foundry.kernel.archive import cell_to_str
        self._elites = list(elites)
        self.grid = {cell_to_str(tuple(g.cell)): g.id for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._elites)


def test_none_elite_is_never_chosen_as_parent():
    # A NONE elite with an absurdly high (volatile) score sits beside a modest
    # real-profession elite. A fitness-only picker would prefer the NONE one;
    # choose_parent must NEVER return it.
    none_g = _mk("g_none", ("NONE", 0), [999.0, 999.0, 999.0])
    real_g = _mk("g_combat", ("COMBAT", 1), [10.0, 11.0, 12.0])
    arc = _FakeArchive([none_g, real_g])

    for seed in range(200):
        pick = choose_parent(arc, seed=seed)
        assert pick is not None
        assert pick.id == real_g.id, f"seed {seed} picked NONE elite {pick.id}"
        assert pick.cell[0] != uoconst.NONE


def test_multiple_real_elites_all_drawable_none_excluded():
    # With several real-profession elites the NONE one is still excluded, but the
    # real elites all remain reachable (the pool is not collapsed to one).
    none_g = _mk("g_none", ("NONE", 0), [500.0, 500.0, 500.0])
    a = _mk("g_a", ("GATHERING", 0), [20.0, 21.0, 22.0])
    b = _mk("g_b", ("CRAFTING", 1), [20.0, 21.0, 22.0])
    arc = _FakeArchive([none_g, a, b])

    seen = set()
    for seed in range(300):
        pick = choose_parent(arc, seed=seed)
        assert pick.cell[0] != uoconst.NONE
        seen.add(pick.id)
    assert seen == {"g_a", "g_b"}, seen


def test_falls_back_to_none_only_when_grid_is_all_none():
    # Degenerate edge: the grid holds ONLY NONE elites. choose_parent must stay
    # total (return a parent rather than crash on an empty weighted choice) by
    # falling back to the NONE elites — matching suggest_target_cell's fallback.
    n0 = _mk("g_n0", ("NONE", 0), [10.0, 10.0, 10.0])
    n1 = _mk("g_n1", ("NONE", 1), [12.0, 12.0, 12.0])
    arc = _FakeArchive([n0, n1])

    pick = choose_parent(arc, seed=0)
    assert pick is not None
    assert pick.id in {"g_n0", "g_n1"}


def test_empty_grid_returns_none():
    arc = _FakeArchive([])
    assert choose_parent(arc, seed=0) is None
