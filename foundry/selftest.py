"""Offline kernel composition self-test (no server, no anima, stdlib only).

Proves the kernel pipeline composes end-to-end by treating the recorded sample
trajectories as if they were eval outputs: parse -> fitness -> descriptor ->
archive (MAP-Elites insertion). Prints the resulting grid and asserts basic
invariants. Run:

    python3 -m foundry.selftest
"""

from __future__ import annotations

import glob
import shutil
import tempfile
from pathlib import Path

from foundry.kernel.archive import Archive, Genome
from foundry.kernel.descriptor import compute_descriptor
from foundry.kernel.fitness import compute_fitness
from foundry.kernel.trajectory import parse_file

SAMPLES = "data/trajectories/*.jsonl"


def run() -> int:
    files = sorted(glob.glob(SAMPLES))
    if not files:
        print(f"no sample trajectories at {SAMPLES} — run from repo root")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="foundry_selftest_"))
    arc = Archive(tmp / "archive")
    fake_ts = 1_700_000_000.0

    print(f"scoring {len(files)} trajectories -> archive at {tmp}\n")
    for i, f in enumerate(files):
        summ = parse_file(f)
        assert summ.parse_errors == 0, f"parse errors in {f}"
        fit = compute_fitness(summ)
        desc = compute_descriptor(summ)

        g = Genome(
            id=arc.next_id(),
            parent=None,
            code_ref="selftest",
            config={"source": Path(f).name},
            eval={
                "fitness": fit.total,
                "cell": list(desc.cell),
                "descriptor": {
                    "profession": desc.profession_focus,
                    "sociability_bin": desc.sociability_bin,
                    "label": desc.label(),
                },
                "seed": i,
                "trajectory_ref": f,
                "breakdown": fit.as_dict(),
            },
            hypothesis="selftest seed genome",
            ts=fake_ts + i,
        )
        res = arc.add(g)
        print(f"  {g.id}  fit={fit.total:7.3f}  cell={desc.cell!s:24}  -> {res.status}")

    print("\narchive summary:")
    summary = arc.summary()
    for k, v in summary.items():
        if k != "cells":
            print(f"  {k:16} {v}")
    print("  grid cells:")
    for cell, gid in summary["cells"].items():
        g = arc.get(gid)
        print(f"    {cell:28} -> {gid} (fit={g.fitness:.3f})")

    # invariants
    assert arc.filled_cells() >= 1, "no cells filled"
    assert arc.qd_score() >= 0.0
    assert arc.best() is not None
    # re-loading the archive from disk must reproduce the grid
    arc2 = Archive(tmp / "archive")
    assert arc2.grid == arc.grid, "grid did not round-trip to disk"

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nOK — kernel pipeline composes and the grid persists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
