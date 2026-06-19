"""observe()'s "Archive context" panel must describe ONE universe.

The panel prints a "filled cells X/Y, QD-score Q" headline directly above the
"empty cells to explore" list. That empty list comes from select.empty_cells,
which is TARGETABLE-only (the NONE fallback row is excluded — select.py
TARGET_PROFESSIONS). The headline used the kernel's Archive.filled_cells()
(len(grid)) and Archive.qd_score() (sum over EVERY elite), both of which fold in
a NONE-row elite — the high-variance scoring artifact the backlog flags. So the
headline counted ground the empty list calls un-fillable, and the QD-score was a
sum no targetable cell accounts for: the same headline-vs-body contradiction
status.py was hardened against. The headline must be derived from the targetable
universe instead.
"""
from __future__ import annotations

from foundry import observe
from foundry.kernel.archive import Archive, Genome
from foundry.kernel import uoconst
from foundry.select import targetable_cells


def _g(gid: str, cell: tuple, fitness: float) -> Genome:
    return Genome(id=gid, eval={"fitness": fitness, "cell": list(cell)})


def _parse_filled(line: str) -> tuple[int, int, float]:
    # "- filled cells X/Y, QD-score Q"
    frac = [tok for tok in line.split() if "/" in tok][0]
    num, den = frac.split("/")
    qd = float(line.split("QD-score", 1)[1].strip())
    return int(num), int(den.rstrip(",")), qd


def test_none_row_elite_excluded_from_filled_and_qd(tmp_path):
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))             # targetable active cell
    arc.add(_g("g_00002", ("GATHERING", 2), 4.0))          # targetable active cell
    arc.add(_g("g_00003", (uoconst.NONE, 0), 99.0))        # NONE fallback row

    lines = observe._archive_context_lines(arc)
    filled_line = next(ln for ln in lines if ln.startswith("- filled cells"))
    num, den, qd = _parse_filled(filled_line)

    # Denominator is the TARGETABLE universe (matches the empty-cells list)...
    assert den == len(targetable_cells())
    # ...the NONE-row elite is NOT counted (only the two targetable cells)...
    assert num == 2
    # ...and its fitness does NOT inflate the QD-score (10 + 4, not + 99).
    assert qd == 14.0
    # The kernel figures (which the panel used to trust) DO include the NONE
    # elite — proving the panel now diverges from them on purpose.
    assert arc.filled_cells() == 3
    assert arc.qd_score() == 113.0


def test_none_row_cell_never_listed_as_a_target_to_explore(tmp_path):
    # The NONE row is never a target; with every targetable cell still empty it
    # must not appear in the explore list, and the headline must agree (0 filled).
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", (uoconst.NONE, 1), 50.0))

    lines = observe._archive_context_lines(arc)
    text = "\n".join(lines)
    assert uoconst.NONE not in text
    num, den, qd = _parse_filled(
        next(ln for ln in lines if ln.startswith("- filled cells")))
    assert num == 0 and qd == 0.0 and den == len(targetable_cells())


def test_clean_targetable_grid_matches_kernel(tmp_path):
    # With no NONE-row / stray cells the panel figures equal the kernel's.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))

    lines = observe._archive_context_lines(arc)
    num, den, qd = _parse_filled(
        next(ln for ln in lines if ln.startswith("- filled cells")))
    assert num == arc.filled_cells()
    assert qd == round(arc.qd_score(), 3)


def test_filled_count_never_exceeds_targetable_denominator(tmp_path):
    # Numerator must always be a subset of the denominator (no "8/6" headline).
    arc = Archive(tmp_path)
    for i, cell in enumerate(targetable_cells(), start=1):
        arc.add(_g(f"g_{i:05d}", cell, float(i)))
    arc.add(_g("g_99999", (uoconst.NONE, 0), 1000.0))      # stray, must not count

    num, den, qd = _parse_filled(
        next(ln for ln in observe._archive_context_lines(arc)
             if ln.startswith("- filled cells")))
    assert num == len(targetable_cells())
    assert num <= den
