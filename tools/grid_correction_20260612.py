"""One-shot grid correction (2026-06-12): demote ruler-inflation genomes.

The 0x25 produce fix (640049d) revived produce_term with a flaw: every
container-update event re-credited the full stack amount, so stack shrink
(ingot consumption, bandage use) and drop-bounces minted produce score.
Fixed by delta-crediting (b858f9c). Held-out re-evals under the fixed ruler
showed the affected genomes do not replicate (median ratio 0.03).

This script — run only after explicit human approval per FOUNDRY.md §9.6 —
1. rewrites each affected genome's eval.fitness to its held-out mean,
   preserving the inflated value and the evidence in the genome record;
2. re-points grid cells at the best corrected genome;
3. vacates (CRAFTING, 2): its only occupant drifted to CRAFTING|1 held-out.

Usage:  uv run python tools/grid_correction_20260612.py [--apply]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARC = ROOT / "foundry" / "archive"

# genome id -> (held_out_mean, per_seed, held_out_cell)
HELD_OUT = {
    "g_00049": (10.736, [9.5, 5.22, 17.49], ["GATHERING", 0]),
    "g_00051": (8.742, [7.94, 9.98, 8.31], ["CRAFTING", 0]),
    "g_00050": (17.132, [21.23, 13.26, 16.9], ["CRAFTING", 1]),
    "g_00052": (3.062, [3.37, 2.71, 3.1], ["CRAFTING", 1]),  # drifted from |2
    "g_00042": (16.175, [17.33, 14.95, 16.25], ["CRAFTING", 0]),
    # g_00041 appended after its re-eval completes
}

# cell -> corrected occupant (None = vacate)
GRID_POINTING = {
    "GATHERING|0": "g_00049",   # 10.74 still beats g_00033's 9.22
    "CRAFTING|0": "g_00042",    # 16.18 beats g_00051's 8.74
    "CRAFTING|1": "g_00050",    # 17.13 keeps the cell
    "CRAFTING|2": None,         # honest vacancy — no genome earned it
}


def main(apply: bool) -> int:
    for gid, (mean, seeds, cell) in HELD_OUT.items():
        p = ARC / "genomes" / f"{gid}.json"
        g = json.loads(p.read_text())
        old = g["eval"]["fitness"]
        print(f"{gid}: fitness {old:.3f} -> {mean:.3f} (held-out cell {cell})")
        if apply:
            g["eval"]["fitness_inflated_ruler"] = old
            g["eval"]["fitness"] = mean
            g["eval"]["held_out_correction"] = {
                "date": "2026-06-12",
                "ruler": "b858f9c delta-credit",
                "per_seed": seeds,
                "held_out_cell": cell,
                "reason": "produce_term stack-shrink/bounce re-credit inflation",
            }
            p.write_text(json.dumps(g, indent=1))

    grid_path = ARC / "grid.json"
    grid = json.loads(grid_path.read_text())
    for cell, gid in GRID_POINTING.items():
        cur = grid.get(cell)
        if gid is None:
            print(f"{cell}: {cur} -> VACATED")
            if apply:
                grid.pop(cell, None)
        elif cur != gid:
            print(f"{cell}: {cur} -> {gid}")
            if apply:
                grid[cell] = gid
        else:
            print(f"{cell}: {cur} (fitness corrected in place)")
    if apply:
        grid_path.write_text(json.dumps(grid, indent=1))
        print("APPLIED.")
    else:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
