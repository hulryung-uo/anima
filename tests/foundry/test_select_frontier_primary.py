"""choose_parent's frontier bias must stay PRIMARY over the quality tiebreak.

The module's whole premise (its top docstring) is that pure fitness-greedy
selection collapses MAP-Elites diversity, so parents are biased toward the
frontier (elites next to EMPTY cells) with quality only a *mild tiebreak*.

The old weight ``1.0 + 2.0*fp + 0.1*quality`` honoured that only for toy
fitnesses: on the live grid quality spans ~[0, 235], so ``0.1*quality`` reached
~23 and dwarfed the ≤4.0 frontier bonus — making selection effectively
fitness-greedy and defeating the frontier bias. These tests pin the corrected,
frontier-primary behaviour while keeping quality a real (bounded) tiebreak.
"""
from foundry.kernel.archive import Archive, Genome, cell_to_str
from foundry.select import (
    _FRONTIER_STEP,
    _quality_tiebreak,
    choose_parent,
)


def _mk(gid: str, cell: tuple, fit: float) -> Genome:
    return Genome(id=gid, eval={"fitness": fit, "cell": list(cell),
                                "per_seed_fitness": [fit]})


def test_quality_tiebreak_never_outvotes_one_frontier_step():
    # Even an unbounded-fitness elite's quality bonus stays below a single
    # fillable neighbour's frontier weight, so frontier ordering is preserved.
    for q in (0.0, 20.0, 100.0, 235.0, 1e6):
        assert 0.0 <= _quality_tiebreak(q) < _FRONTIER_STEP
    # ... and it is monotone (a higher-quality elite still wins among peers).
    assert _quality_tiebreak(10.0) < _quality_tiebreak(50.0) < _quality_tiebreak(200.0)


def test_frontier_elite_beats_higher_fitness_interior_elite(tmp_path):
    # Grid: a modest-fitness COMBAT elite with BOTH soc neighbours open (real
    # frontier 2) vs a much-higher-fitness MAGIC elite boxed in by filled
    # neighbours (frontier 0). The frontier elite must be picked far more often;
    # under the old 0.1*fitness term the 230-fitness interior elite (bonus ~23)
    # swamped the frontier elite's ≤4.0 bonus and won the majority.
    arc = Archive(tmp_path)
    frontier = _mk("g_front", ("COMBAT", 1), 20.0)        # frontier 2, low fitness
    # Box the MAGIC row so the high-fitness elite has NO open neighbour.
    interior = _mk("g_inter", ("MAGIC", 1), 230.0)        # frontier 0, high fitness
    mag0 = _mk("g_m0", ("MAGIC", 0), 5.0)
    mag2 = _mk("g_m2", ("MAGIC", 2), 5.0)
    for g in (frontier, interior, mag0, mag2):
        arc.save_genome(g)
        arc.grid[cell_to_str(g.cell)] = g.id

    counts = {"g_front": 0, "g_inter": 0, "g_m0": 0, "g_m2": 0}
    for seed in range(600):
        counts[choose_parent(arc, seed=seed).id] += 1

    # Frontier-primary: the low-fitness frontier elite must clearly beat the
    # high-fitness boxed-in elite (the bug had it the other way around).
    assert counts["g_front"] > counts["g_inter"], counts


def test_quality_still_breaks_ties_at_equal_frontier(tmp_path):
    # Two COMBAT elites with IDENTICAL frontier (both have one open neighbour):
    # the higher-quality one should be favoured (the tiebreak is mild but real).
    arc = Archive(tmp_path)
    # COMBAT|0 and COMBAT|2 each border exactly one empty cell (COMBAT|1 is the
    # only open COMBAT bin) → equal frontier_potential == 1.
    lo = _mk("g_lo", ("COMBAT", 0), 30.0)
    hi = _mk("g_hi", ("COMBAT", 2), 200.0)
    # Fill an unrelated row so the grid isn't trivial, and keep COMBAT|1 empty.
    other = _mk("g_other", ("MAGIC", 0), 50.0)
    for g in (lo, hi, other):
        arc.save_genome(g)
        arc.grid[cell_to_str(g.cell)] = g.id

    counts = {"g_lo": 0, "g_hi": 0}
    for seed in range(600):
        pick = choose_parent(arc, seed=seed)
        if pick.id in counts:
            counts[pick.id] += 1
    # Equal frontier → the higher-quality elite wins the tiebreak more often.
    assert counts["g_hi"] > counts["g_lo"], counts


def test_choose_parent_still_total_on_all_equal(tmp_path):
    arc = Archive(tmp_path)
    g = _mk("g_only", ("COMBAT", 1), 0.0)
    arc.save_genome(g)
    arc.grid[cell_to_str(g.cell)] = g.id
    pick = choose_parent(arc, seed=0)
    assert pick is not None and pick.id == "g_only"
