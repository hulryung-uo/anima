"""The selection policy must never AIM a mutation at the NONE fallback row.

uoconst.NONE is the descriptor's "gained no livelihood skill" fallback — a
failed/degenerate agent, not a behavioral niche. It leaked into the target grid
via uoconst.PROFESSION_BINS, so both targeting paths could hand the mutator an
incoherent goal ("land in cell NONE|x") or burn an IMPROVE cycle re-tuning a
failed-profession elite. empty_cells()/suggest_target_cell() must skip NONE; the
kernel archive may still RECORD a NONE elite (promotion is kernel-owned), and
all_active_cells() (the status denominator) must still enumerate it.
"""
from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, Genome, cell_to_str
from foundry.select import (
    SOC_BINS,
    TARGET_PROFESSIONS,
    all_active_cells,
    empty_cells,
    suggest_target_cell,
    targetable_cells,
)


def _mk(gid: str, cell: tuple, fit: float) -> Genome:
    return Genome(id=gid, eval={"fitness": fit, "cell": list(cell),
                                "per_seed_fitness": [fit]})


def test_target_grid_excludes_none_but_active_grid_keeps_it():
    # NONE is a real bin in the kernel descriptor space (and the display grid)…
    assert uoconst.NONE in uoconst.PROFESSION_BINS
    assert any(c[0] == uoconst.NONE for c in all_active_cells())
    # …but the policy never aims at it.
    assert uoconst.NONE not in TARGET_PROFESSIONS
    assert all(c[0] != uoconst.NONE for c in targetable_cells())
    # The full grid is strictly larger than the targetable grid by exactly one
    # profession row (so status.py's denominator is unchanged).
    assert len(all_active_cells()) == len(targetable_cells()) + SOC_BINS


def test_empty_cells_never_lists_a_none_cell(tmp_path):
    arc = Archive(tmp_path)
    # Empty archive: every targetable cell is open, but no NONE cell is offered.
    empties = empty_cells(arc)
    assert empties, "fresh archive should expose explore targets"
    assert all(c[0] != uoconst.NONE for c in empties)
    assert ("GATHERING", 0) in empties
    assert ("NONE", 0) not in empties


def test_empty_branch_target_is_never_none(tmp_path):
    # Fill the whole NON-NONE grid except one COMBAT cell, leaving the NONE row
    # entirely empty. The only legitimate explore target is the COMBAT hole —
    # the NONE cells must NOT be offered even though they are empty.
    arc = Archive(tmp_path)
    hole = ("COMBAT", 2)
    for c in targetable_cells():
        if c == hole:
            continue
        g = _mk(f"g_{cell_to_str(c)}", c, 50.0)
        arc.save_genome(g)
        arc.grid[cell_to_str(c)] = g.id
    # Sanity: NONE cells are empty in the grid but excluded from targeting.
    assert all(cell_to_str(("NONE", s)) not in arc.grid for s in range(SOC_BINS))
    for seed in range(40):
        out = suggest_target_cell(arc, seed=seed)
        assert out == hole, f"seed {seed} aimed at {out}, not the COMBAT hole"


def test_full_grid_branch_never_improves_a_none_elite(tmp_path):
    # FULL targetable grid (every non-NONE cell filled) PLUS a recorded NONE
    # elite. The full-grid headroom branch must pick a real profession cell to
    # re-improve, never the NONE elite.
    arc = Archive(tmp_path)
    for c in targetable_cells():
        g = _mk(f"g_{cell_to_str(c)}", c, 50.0)
        arc.save_genome(g)
        arc.grid[cell_to_str(c)] = g.id
    # A NONE elite exists in the grid (the kernel recorded it) — it just must
    # not be a target.
    none_cell = ("NONE", 0)
    ng = _mk("g_none", none_cell, 999.0)  # absurdly high so a fitness-only
    arc.save_genome(ng)                   # picker would prefer it
    arc.grid[cell_to_str(none_cell)] = ng.id
    # No empty targetable cell remains → full-grid branch.
    assert empty_cells(arc) == []
    for seed in range(40):
        out = suggest_target_cell(arc, seed=seed)
        assert out is not None
        assert out[0] != uoconst.NONE, f"seed {seed} aimed at NONE elite {out}"


def test_full_grid_branch_falls_back_when_only_none_elites(tmp_path):
    # Degenerate edge: the grid holds ONLY NONE elites and is otherwise full of
    # nothing targetable. The branch must still return a cell (not crash on an
    # empty weighted choice) — the fallback keeps it total.
    arc = Archive(tmp_path)
    for s in range(SOC_BINS):
        c = ("NONE", s)
        g = _mk(f"g_none_{s}", c, 10.0 + s)
        arc.save_genome(g)
        arc.grid[cell_to_str(c)] = g.id
    # Force the FULL-GRID branch: pretend no targetable empties remain by
    # filling them too (so empty_cells() == []). We only need the branch to be
    # total and return a valid recorded cell.
    for c in targetable_cells():
        g = _mk(f"g_{cell_to_str(c)}", c, 1.0)
        arc.save_genome(g)
        arc.grid[cell_to_str(c)] = g.id
    assert empty_cells(arc) == []
    out = suggest_target_cell(arc, seed=0)
    assert out is not None
    assert cell_to_str(out) in arc.grid
