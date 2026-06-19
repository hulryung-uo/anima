"""Archive status view — the QD grid, elites, and lineage at a glance.

    python3 -m foundry.status [archive_root]

Mutator-editable (presentation only; reads the kernel archive, changes nothing).
"""

from __future__ import annotations

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, cell_to_str
from foundry.select import SOC_BINS, all_active_cells, targetable_cells

SOC_LABELS = ("low", "mid", "high")


def render(arc: Archive) -> str:
    lines: list[str] = []
    s = arc.summary()
    # Count filled cells against the SAME universe the grid table renders
    # (all_active_cells()), not the raw grid size. The kernel's
    # summary()["filled_cells"] is len(grid), which can include a cell whose
    # profession/sociability bin is NOT in the active enumeration (e.g. a
    # profession dropped or renamed across an evolution). Such a cell inflates
    # the numerator -- even past the denominator (e.g. "22/21") -- yet is
    # silently absent from the table below, so the headline contradicts the
    # body. Intersect so numerator is a subset of the denominator and matches
    # exactly the cells rendered.
    active = {cell_to_str(c) for c in all_active_cells()}
    filled = sum(1 for k in arc.grid if k in active)
    active_elites = [g for g in arc.elites() if cell_to_str(g.cell) in active]
    # qd-score and best summarise RUN QUALITY, so they exclude the NONE fallback
    # row (R33: NONE is not a real niche -- a failed/wandered agent that banked a
    # high-variance score, never a behavioural cell exploration aims at). A
    # volatile NONE-row elite would otherwise skew the headline qd-score (a sum)
    # and could claim "best" (a max) for a row no targetable lineage occupies --
    # the very skew commit 25ad63f set out to remove, which slipped through by
    # reusing the NONE-inclusive `active` set instead of the targetable one. They
    # also still drop any stray cell outside the active enumeration (a dropped or
    # renamed profession). The filled count keeps the full active denominator
    # (NONE is still a real, if degenerate, grid cell the table renders), so the
    # figures diverge on purpose: filled describes grid COVERAGE, qd-score/best
    # describe QUALITY.
    targetable = {cell_to_str(c) for c in targetable_cells()}
    quality_elites = [g for g in active_elites if cell_to_str(g.cell) in targetable]
    qd_score = round(sum(g.fitness for g in quality_elites), 3)
    best_fitness = round(max((g.fitness for g in quality_elites), default=0.0), 3)
    lines.append(
        f"genomes {s['total_genomes']}  filled {filled}/{len(active)}"
        f"  qd-score {qd_score}  best {best_fitness}"
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
    # Iterate the SAME quality-elite set the headline qd-score/best summarise --
    # the targetable elites (active cells, MINUS the NONE fallback row) -- NOT
    # active_elites and NOT the raw arc.elites(). Two cells must stay out of this
    # listing: (1) one whose profession/sociability is outside the active
    # enumeration (a profession dropped or renamed across an evolution), and
    # (2) the NONE fallback row (R33: not a real niche -- a failed/wandered agent
    # that banked a high-variance score). Either one, listed HERE and -- since
    # the listing is sorted by fitness -- possibly AT THE TOP, reintroduces the
    # headline-vs-body contradiction the filled/qd-score/best fixes removed: the
    # report would crown a "best" lineage the headline and grid both deny. R51
    # narrowed this from arc.elites() but stopped at active_elites (still
    # NONE-inclusive), so a top-fitness NONE elite still headed the list while
    # the headline best excluded it; use quality_elites so the two agree.
    lines.append("elites (lineage ← parents):")
    for g in sorted(quality_elites, key=lambda g: -g.fitness):
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
