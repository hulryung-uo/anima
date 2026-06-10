"""Archive status view — the QD grid, elites, and lineage at a glance.

    python3 -m foundry.status [archive_root]

Mutator-editable (presentation only; reads the kernel archive, changes nothing).
"""

from __future__ import annotations

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive
from foundry.select import SOC_BINS, all_active_cells

SOC_LABELS = ("low", "mid", "high")


def render(arc: Archive) -> str:
    lines: list[str] = []
    s = arc.summary()
    lines.append(
        f"genomes {s['total_genomes']}  filled {s['filled_cells']}/{len(all_active_cells())}"
        f"  qd-score {s['qd_score']}  best {s['best_fitness']}"
    )
    lines.append("")

    # grid table: rows = profession, cols = sociability bins
    colw = 18
    header = "profession".ljust(14) + "".join(
        f"soc-{SOC_LABELS[b]}".ljust(colw) for b in range(SOC_BINS))
    lines.append(header)
    lines.append("-" * len(header))
    for prof in uoconst.PROFESSION_BINS:
        row = prof.ljust(14)
        for b in range(SOC_BINS):
            g = arc.get_elite((prof, b))
            cellt = f"{g.id} {g.fitness:7.2f}" if g else "·"
            row += cellt.ljust(colw)
        lines.append(row)
    lines.append("")

    # elites with lineage chains
    lines.append("elites (lineage ← parents):")
    for g in sorted(arc.elites(), key=lambda g: -g.fitness):
        chain = []
        cur = g
        seen = set()
        while cur and cur.id not in seen and len(chain) < 8:
            seen.add(cur.id)
            chain.append(cur.id)
            cur = arc.get(cur.parent) if cur.parent else None
        per_seed = g.eval.get("per_seed_fitness") or []
        seeds = f" seeds={[round(v, 2) for v in per_seed]}" if len(per_seed) > 1 else ""
        lines.append(f"  {g.fitness:8.3f}  {' ← '.join(chain)}{seeds}")
        if g.hypothesis:
            lines.append(f"            “{g.hypothesis}”")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    root = argv[0] if argv else "foundry/archive"
    print(render(Archive(root)))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
