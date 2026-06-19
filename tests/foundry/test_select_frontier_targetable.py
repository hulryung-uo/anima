"""The parent-selection frontier bias must only count TARGETABLE empty neighbors.

``choose_parent`` biases toward elites adjacent to EMPTY cells so a mutation is
likely to fill new ground. But ``empty_cells``/``suggest_target_cell`` never aim
at the NONE fallback row (see ``test_select_no_none_target.py``), so an empty
NONE-row neighbour is NOT fillable ground. ``_frontier_potential`` used to count
every empty neighbour, including NONE cells — which (a) inflated the frontier
score for proximity to ground exploration will never claim and (b) handed a full
frontier bonus to degenerate NONE-row elites (whose entire neighbourhood is the
un-fillable NONE row), favouring a failed/wandering genome as the next parent.

These tests pin the corrected, target-consistent behaviour.
"""
from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, Genome, cell_to_str
from foundry.select import (
    SOC_BINS,
    _frontier_potential,
    choose_parent,
    targetable_cells,
)


def _mk(gid: str, cell: tuple, fit: float) -> Genome:
    return Genome(id=gid, eval={"fitness": fit, "cell": list(cell),
                                "per_seed_fitness": [fit]})


def test_none_row_neighbors_are_not_frontier():
    # A NONE-row elite's neighbours are all NONE cells — un-fillable ground.
    # With nothing filled, the OLD code scored frontier == (#empty soc neighbours)
    # == 1 for ("NONE", 0); the fixed code scores 0 (no targetable neighbour).
    assert _frontier_potential(("NONE", 0), filled=set()) == 0
    assert _frontier_potential(("NONE", 1), filled=set()) == 0


def test_real_profession_frontier_still_counts_targetable_empties():
    # A COMBAT mid-bin elite with both soc neighbours empty has frontier 2;
    # fill one and it drops to 1. (Targetable cells are still counted.)
    assert _frontier_potential(("COMBAT", 1), filled=set()) == 2
    filled = {cell_to_str(("COMBAT", 0))}
    assert _frontier_potential(("COMBAT", 1), filled=filled) == 1
    filled |= {cell_to_str(("COMBAT", 2))}
    assert _frontier_potential(("COMBAT", 1), filled=filled) == 0


def test_frontier_only_counts_targetable_neighbors():
    # Every neighbour _frontier_potential ever credits must be a targetable cell.
    targetable = {cell_to_str(c) for c in targetable_cells()}
    for prof in uoconst.PROFESSION_BINS:
        for soc in range(SOC_BINS):
            cell = (prof, soc)
            # Reconstruct which neighbours got counted by toggling `filled`.
            empty = _frontier_potential(cell, filled=set())
            # Fill the whole targetable grid → no targetable neighbour remains.
            all_filled = set(targetable)
            assert _frontier_potential(cell, filled=all_filled) == 0
            if prof == uoconst.NONE:
                assert empty == 0, f"{cell} credited an un-fillable neighbour"
            else:
                # All counted neighbours were targetable (else the all-filled
                # case above could not have driven the count to 0).
                assert empty >= 0


def test_choose_parent_does_not_favor_a_degenerate_none_elite(tmp_path):
    # Grid: one solid COMBAT elite (its soc neighbours OPEN → real frontier) and
    # one NONE-row elite (neighbours all NONE → no real frontier). Across many
    # seeds the working profession elite must be picked at least as often as the
    # degenerate NONE one — the OLD frontier count gave the NONE elite an equal
    # (often larger) frontier bonus and could draw it the majority of the time.
    arc = Archive(tmp_path)
    combat = _mk("g_00001", ("COMBAT", 1), 20.0)
    none_g = _mk("g_00002", ("NONE", 1), 20.0)
    for g in (combat, none_g):
        arc.save_genome(g)
        arc.grid[cell_to_str(g.cell)] = g.id

    counts = {"g_00001": 0, "g_00002": 0}
    for seed in range(400):
        pick = choose_parent(arc, seed=seed)
        counts[pick.id] += 1

    # The COMBAT elite has frontier 2 (both soc neighbours targetable+empty);
    # the NONE elite now has frontier 0, so the working profession dominates.
    assert counts["g_00001"] > counts["g_00002"], counts


def test_choose_parent_still_returns_a_parent_when_only_none_elites(tmp_path):
    # Degenerate grid of only NONE elites: frontier is now 0 for all of them, but
    # the base weight (1.0 + quality term) keeps choose_parent total — it must
    # still return a valid recorded elite rather than crash on all-zero weights.
    arc = Archive(tmp_path)
    for s in range(SOC_BINS):
        g = _mk(f"g_none_{s}", ("NONE", s), 10.0 + s)
        arc.save_genome(g)
        arc.grid[cell_to_str(g.cell)] = g.id
    pick = choose_parent(arc, seed=0)
    assert pick is not None
    assert cell_to_str(pick.cell) in arc.grid
