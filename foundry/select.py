"""Parent selection for the develop cycle (FOUNDRY.md §6) — mutator-editable.

MAP-Elites is illuminated by *which* parent we mutate next. Pure fitness-greedy
selection collapses diversity; pure random ignores the frontier. This policy
biases toward elites that sit next to EMPTY cells (so a mutation is likely to
fill new behavioral ground), with fitness as a mild tiebreak. It is on the
mutable side of the kernel boundary: the AI may rewrite this strategy. It may
read the kernel (archive, constants) but must not weaken the archive's
promotion rule (that lives in the kernel).
"""

from __future__ import annotations

import random

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, Genome, cell_to_str

SOC_BINS = 3  # Phase-0 active grid: profession_focus × sociability (3 bins)


def all_active_cells() -> list[tuple]:
    return [(prof, soc) for prof in uoconst.PROFESSION_BINS for soc in range(SOC_BINS)]


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
    filled = set(archive.grid.keys())
    return [c for c in all_active_cells() if cell_to_str(c) not in filled]


def _frontier_potential(cell: tuple, filled: set[str]) -> int:
    return sum(1 for n in _neighbors(cell) if cell_to_str(n) not in filled)


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
        weights.append(1.0 + 2.0 * fp + 0.1 * max(0.0, g.fitness))
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
            # Code-recency multiplier (genome id is monotonic): newer row
            # members carry newer base-code fixes. A 46%-probability draw of
            # an old-code COMBAT elite re-ran the pre-crash-fix combat loop
            # and scored 4.1 vs the fresh seed's 23.9 — fitness weighting
            # alone doesn't protect against stale machinery.
            by_age = sorted(row, key=lambda g: g.id)
            rank = {g.id: i for i, g in enumerate(by_age)}
            n = len(row)
            weights = [
                (1.0 + 0.1 * max(0.0, g.fitness))
                * (1.0 + 2.0 * (rank[g.id] / (n - 1)) if n > 1 else 1.0)
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
    if not empties:
        return None
    row_filled: dict[str, int] = {}
    row_best: dict[str, float] = {}
    for g in archive.elites():
        prof = g.cell[0]
        row_filled[prof] = row_filled.get(prof, 0) + 1
        row_best[prof] = max(row_best.get(prof, 0.0), g.fitness)
    weights = [
        1.0
        + 2.0 * row_filled.get(c[0], 0)              # proven machinery
        + min(2.0, row_best.get(c[0], 0.0) / 20.0)   # row quality, capped
        for c in empties
    ]
    return random.Random(seed).choices(empties, weights=weights, k=1)[0]
