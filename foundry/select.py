"""Parent selection for the develop cycle (FOUNDRY.md §6) — mutator-editable.

MAP-Elites is illuminated by *which* parent we mutate next. Pure fitness-greedy
selection collapses diversity; pure random ignores the frontier. This policy
biases toward elites that sit next to EMPTY cells (so a mutation is likely to
fill new behavioral ground), with RELIABILITY (the variance-aware lower bound
the kernel promotes on) as a mild tiebreak — not the raw fitness mean, which
over-weights lucky high-variance parents the promotion rule would reject. It is on the
mutable side of the kernel boundary: the AI may rewrite this strategy. It may
read the kernel (archive, constants) but must not weaken the archive's
promotion rule (that lives in the kernel).
"""

from __future__ import annotations

import random

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, Genome, cell_to_str

SOC_BINS = 3  # Phase-0 active grid: profession_focus × sociability (3 bins)

# Professions the selection policy may AIM a mutation at. uoconst.NONE is the
# descriptor *fallback* — it means the eval produced no livelihood-skill gain
# (a failed/degenerate agent that just wandered or died), not a behavioral
# niche. Telling the mutator to "land in cell NONE|x" is incoherent (you cannot
# deliberately gain no profession), and the backlog flags NONE cells as a
# high-variance scoring artifact (g_00118 failed its profession, wandered, and
# banked a volatile NONE score). The kernel archive still RECORDS a NONE elite
# if one evals there (promotion is kernel-owned and untouched); we just never
# spend an EXPLORE/IMPROVE cycle reaching for one. all_active_cells() keeps the
# full enumeration so status.py's filled-cell denominator is unchanged.
TARGET_PROFESSIONS: tuple[str, ...] = tuple(
    p for p in uoconst.PROFESSION_BINS if p != uoconst.NONE
)


def _selection_quality(g: Genome) -> float:
    """Variance-aware parent quality that ALSO honors human held-out corrections.

    reliability (mean − λ·pstdev, recomputed from per_seed_fitness) is the right
    conservative signal for normal genomes — but it bypasses the authoritative
    ``eval.fitness`` scalar, which the human grid-correction tool overwrote to a
    held-out value for the ruler-inflation genomes (2026-06-12; markers
    ``fitness_inflated_ruler`` / ``held_out_correction`` in the records). Their
    per_seed data is the PRESERVED INFLATED evidence, so reliability re-inflates
    them (e.g. g_00052 held-out 3.06 → reliability 174.9). min() picks the
    held-out value when one exists and the variance-discounted bound otherwise.
    """
    return min(g.fitness, g.reliability)


def _row_best_quality(elites: list[Genome]) -> dict[str, float]:
    """True per-profession best selection-quality, honoring negative values.

    A genome's _selection_quality can be < 0 (a low-fitness, high-variance
    elite: reliability = mean − λ·pstdev drops below zero). Seeding the running
    max at 0.0 poisons any row whose elites are all negative — it reports a
    phantom 0.0 ceiling that no genome reached, which then mis-states the
    FULL-GRID "gap to row best" so the row-best cell (true headroom 0) gets a
    positive, inflated exploration weight. Take the real max instead.
    """
    best: dict[str, float] = {}
    for g in elites:
        prof = g.cell[0]
        q = _selection_quality(g)
        best[prof] = q if prof not in best else max(best[prof], q)
    return best


def all_active_cells() -> list[tuple]:
    """Every grid cell (incl. the NONE fallback row) — display/denominator use."""
    return [(prof, soc) for prof in uoconst.PROFESSION_BINS for soc in range(SOC_BINS)]


def targetable_cells() -> list[tuple]:
    """Cells a mutation may be aimed at (excludes the NONE fallback row)."""
    return [(prof, soc) for prof in TARGET_PROFESSIONS for soc in range(SOC_BINS)]


def _neighbors(cell: tuple) -> list[tuple]:
    """Ordinal neighbors along sociability (profession is categorical, no order)."""
    prof, soc = cell
    out = []
    for ds in (-1, 1):
        ns = soc + ds
        if 0 <= ns < SOC_BINS:
            out.append((prof, ns))
    return out


def empty_cells(archive: Archive) -> list[tuple]:
    """Empty cells worth exploring — never the NONE fallback row (see
    TARGET_PROFESSIONS). This list feeds both suggest_target_cell and the
    observe-step 'empty cells to explore' prompt."""
    filled = set(archive.grid.keys())
    return [c for c in targetable_cells() if cell_to_str(c) not in filled]


def _frontier_potential(cell: tuple, filled: set[str]) -> int:
    """Count this cell's neighbors that an EXPLORE cycle could actually fill.

    The frontier bias exists to favour parents sitting next to fillable ground,
    so the count must match what ``empty_cells``/``suggest_target_cell`` will ever
    aim at: a *targetable* empty neighbour. A neighbour in the NONE fallback row
    is empty but never a target (see TARGET_PROFESSIONS) — counting it inflates
    the frontier score for proximity to ground exploration will never claim, and
    in particular over-weights degenerate NONE-row elites as parents (their whole
    neighbourhood is the un-fillable NONE row, so they'd otherwise score a full
    frontier bonus). Restrict the count to targetable empties so the parent-side
    signal is consistent with the target-side NONE exclusion.
    """
    targetable = {cell_to_str(c) for c in targetable_cells()}
    return sum(1 for n in _neighbors(cell)
               if cell_to_str(n) in targetable and cell_to_str(n) not in filled)


def choose_parent(archive: Archive, seed: int = 0) -> Genome | None:
    """Pick a parent elite to mutate, biased toward the frontier.

    Returns None when the grid is empty (caller should seed from a base genome).
    """
    elites = archive.elites()
    if not elites:
        return None
    filled = set(archive.grid.keys())
    rng = random.Random(seed)
    weights = []
    for g in elites:
        fp = _frontier_potential(g.cell, filled)
        # variance-aware quality that honors held-out corrections (see
        # _selection_quality) — don't favour volatile lucky / re-inflated parents.
        weights.append(1.0 + 2.0 * fp + 0.1 * max(0.0, _selection_quality(g)))
    return rng.choices(elites, weights=weights, k=1)[0]


def choose_parent_for_target(archive: Archive, target: tuple | None,
                             seed: int = 0) -> Genome | None:
    """Pick a parent matched to the EXPLORE target's profession row.

    Parent and target used to be chosen independently, which paired
    e.g. a COMBAT|2 target with a bard-lineage parent whose checked-out
    code predates the combat fixes — the eval then ran broken machinery
    (observed: two COMBAT|2 cycles landing NONE on parents whose code
    still had the 0x0B session-crash). The same-row elite IS the working
    machinery for that profession, and the freshest one carries the
    newest base-code fixes.
    """
    if target:
        row = [g for g in archive.elites() if g.cell and g.cell[0] == target[0]]
        if row:
            rng = random.Random(seed)
            # Code-recency tiebreak (genome id is monotonic): newer row
            # members carry newer base-code fixes. This nudges TOWARD the
            # fresher lineage but must NOT eclipse selection quality — a ×3
            # multiplier (the old 2.0 coefficient) drowned the ×0.1 quality
            # term, so the NEWEST row member won even when it was several-fold
            # worse and steadier elites (or human held-out demotions) sat right
            # beside it, defeating the variance-aware / held-out-correction
            # guarantee _selection_quality is supposed to provide. A small
            # additive-scale coefficient keeps recency a genuine mild tiebreak
            # (matching how the module treats every secondary signal): among
            # near-equal-quality elites the fresher one is still favoured, but
            # a clearly better parent is no longer outvoted on age alone.
            by_age = sorted(row, key=lambda g: g.id)
            rank = {g.id: i for i, g in enumerate(by_age)}
            n = len(row)
            weights = [
                (1.0 + 0.1 * max(0.0, _selection_quality(g)))
                * (1.0 + 0.5 * (rank[g.id] / (n - 1)) if n > 1 else 1.0)
                for g in row
            ]
            return rng.choices(row, weights=weights, k=1)[0]
    return choose_parent(archive, seed=seed)


def suggest_target_cell(archive: Archive, seed: int = 0) -> tuple | None:
    """An empty active cell to aim an EXPLORE-type mutation at (or None if full).

    Weighted toward "easy" rows — professions whose loop is already PROVEN
    (more filled cells, higher best fitness in the row). Reaching the last
    bin of a working profession is a small behavioral tweak; cracking an
    unproven profession is a capability problem. Spend exploration on the
    cheap wins first (and previously: 4 consecutive CRAFTING|2 attempts
    failed on a broken craft primitive while COMBAT|2 sat untried).
    """
    empties = empty_cells(archive)
    row_filled: dict[str, int] = {}
    elites_all = archive.elites()
    row_best = _row_best_quality(elites_all)
    for g in elites_all:
        prof = g.cell[0]
        row_filled[prof] = row_filled.get(prof, 0) + 1
    if not empties:
        # FULL GRID: target the cells with the largest gap to their row's
        # best — proven headroom (the row best's machinery exists; it just
        # hasn't been adapted to this cell's sociability bin). Returning a
        # target (instead of None) keeps the row-matched/recency parent
        # pairing in play; IMPROVE cycles on bare frontier draws kept
        # handing weak cells to stale-code lineages (g_00068: 6.9 on a
        # pre-crash-fix parent while the row's fresh seed sat at 55.9).
        elites = archive.elites()
        if not elites:
            return None
        # Don't aim an IMPROVE cycle at a NONE-fallback elite (see
        # TARGET_PROFESSIONS); fall back to the unfiltered set only if the grid
        # somehow holds nothing but NONE elites.
        cells = [g.cell for g in elites if g.cell and g.cell[0] != uoconst.NONE]
        cells = cells or [g.cell for g in elites]
        weights = [
            1.0 + max(0.0, row_best[c[0]] - _selection_quality(archive.get_elite(c)))
            for c in cells
        ]
        return random.Random(seed).choices(cells, weights=weights, k=1)[0]
    weights = [
        1.0
        + 2.0 * row_filled.get(c[0], 0)              # proven machinery
        + min(2.0, max(0.0, row_best.get(c[0], 0.0)) / 20.0)   # row quality, capped (no negative bonus)
        for c in empties
    ]
    return random.Random(seed).choices(empties, weights=weights, k=1)[0]
