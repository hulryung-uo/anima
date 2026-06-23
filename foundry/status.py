"""Archive status view — the QD grid, elites, and lineage at a glance.

    python3 -m foundry.status [archive_root]

Mutator-editable (presentation only; reads the kernel archive, changes nothing).
"""

from __future__ import annotations

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, cell_to_str
from foundry.select import SOC_BINS, _selection_quality, all_active_cells, targetable_cells

SOC_LABELS = ("low", "mid", "high")


def _round_seeds(per_seed: list) -> list[float]:
    """Render-safe rounding of per-seed fitness values.

    The kernel's reliability_score() coerces each per-seed entry with float()
    before use, so a JSON-loaded genome can legitimately carry a float-accepted
    STRING like "50.0" in per_seed_fitness and still survive promotion. The
    status report, however, fed those raw values straight to round(), and
    round("50.0", 2) raises 'type str doesn't define __round__' -- crashing the
    whole report on one stray string. Coerce to float first (the same contract
    the kernel already uses), skipping anything genuinely non-numeric so a
    malformed entry degrades one seed instead of the entire report.
    """
    out: list[float] = []
    for v in per_seed:
        try:
            out.append(round(float(v), 2))
        except (TypeError, ValueError):
            continue
    return out


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
    # The headline "best" crowns ONE genome as the run's apparent champion, so it
    # must read the SAME variance-aware trust signal the lineage listing below is
    # sorted/led by (and that select.choose_parent picks parents on, reeval
    # --elites re-checks in, and observe.history's "PROVEN recipes" list leads
    # with -- 985ff44): _selection_quality = min(fitness, reliability) (reliability
    # = mean - lambda*pstdev). max(g.fitness) is the raw MEAN, which floats a lucky
    # high-variance elite (per_seed [400, 0] -> mean 200 but reliability 0) to the
    # headline as "best 200.000" even though that same elite sorts LAST in the
    # lineage and every other consumer distrusts it -- the exact display-vs-trust
    # contradiction the lineage sort already removed. qd-score stays a sum of the
    # raw means (a sum summarises aggregate coverage, not a single champion).
    best_fitness = round(max((_selection_quality(g) for g in quality_elites),
                             default=0.0), 3)
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
            # Show the SAME variance-aware trust signal the headline ``best`` and
            # the lineage listing use (_selection_quality = min(fitness,
            # reliability)), NOT the raw fitness mean. The grid is the spatial map
            # of the run, and the file's own consistency invariant says a lucky
            # high-variance elite must not advertise a number "the headline AND
            # GRID both deny". A cell still rendering g.fitness floats a coin-flip
            # elite (per_seed [400, 0] -> mean 200 but reliability/quality 0) as a
            # "200.00" cell while the headline best (50) and the lineage (q 0.00)
            # both rank it last -- the exact display-vs-trust contradiction the
            # headline/lineage fixes removed, leaking through the table. Display
            # the quality so every face of the report describes one universe.
            cellt = f"{g.id} {_selection_quality(g):7.2f}" if g else "·"
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
    #
    # ORDER (and lead) the listing by the variance-aware trust signal
    # _selection_quality = min(fitness, reliability) (reliability = mean -
    # lambda*pstdev) -- the SAME signal select.py picks parents on, reeval
    # --elites re-checks in, and observe.history's "PROVEN recipes" list was
    # migrated to (985ff44). Sorting by the raw fitness MEAN floats a lucky
    # high-variance elite (per_seed [400, 0] -> mean 200 but reliability 0) to
    # the TOP of the operator-facing lineage, advertising as the run's best a
    # recipe every other consumer ranks last. Lead with q... so the displayed
    # number and the ordering agree, and keep the raw mean visible (labelled) so
    # no information is lost -- the display-matches-trust consistency
    # observe.history already enforces.
    lines.append("elites (lineage ← parents):")
    for g in sorted(quality_elites, key=lambda g: -_selection_quality(g)):
        chain = []
        cur = g
        seen = set()
        while cur and cur.id not in seen and len(chain) < 8:
            seen.add(cur.id)
            chain.append(cur.id)
            cur = arc.get(cur.parent) if cur.parent else None
        per_seed = g.eval.get("per_seed_fitness") or []
        seeds = f" seeds={_round_seeds(per_seed)}" if len(per_seed) > 1 else ""
        lines.append(f"  q{_selection_quality(g):8.3f}  (mean {g.fitness:8.3f})  "
                     f"{' ← '.join(chain)}{seeds}")
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
