"""status.py headline filled-cell count must match the cells the table renders.

The kernel's summary()["filled_cells"] is len(grid). The grid can hold a cell
whose profession/sociability is NOT in the active enumeration (a profession
dropped or renamed across an evolution). Such a cell inflates the headline
numerator -- potentially past the denominator -- yet is silently absent from the
grid table render() prints below it, so the report contradicts itself. render()
must count filled cells -- AND derive qd-score / best -- against the SAME
active-cell universe it tabulates.
"""
from __future__ import annotations

from foundry import status
from foundry.kernel.archive import Archive, Genome
from foundry.select import all_active_cells


def _g(gid: str, cell: tuple, fitness: float) -> Genome:
    return Genome(id=gid, eval={"fitness": fitness, "cell": list(cell)})


def _headline_filled(header: str) -> tuple[int, int]:
    # "genomes N  filled X/Y  qd-score ..."
    frac = [tok for tok in header.split() if "/" in tok][0]
    num, den = frac.split("/")
    return int(num), int(den)


def _headline_value(header: str, label: str) -> float:
    # "genomes N  filled X/Y  qd-score Q  best B" -> the token after `label`.
    toks = header.split()
    return float(toks[toks.index(label) + 1])


def _table_filled(rendered: str) -> int:
    # Count genome-id cells inside the GRID TABLE block only (the lines between
    # the "----" separator and the blank line preceding the elites section).
    lines = rendered.splitlines()
    start = next(i for i, ln in enumerate(lines) if set(ln) == {"-"}) + 1
    n = 0
    for ln in lines[start:]:
        if ln == "":
            break
        for tok in ln.split():
            if tok.startswith("g_"):
                n += 1
    return n


def test_filled_count_ignores_cell_outside_active_grid(tmp_path):
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))            # real active cell
    arc.add(_g("g_00002", ("LEGACYPROF", 0), 5.0))        # outside enumeration

    rendered = status.render(arc)
    num, den = _headline_filled(rendered.splitlines()[0])

    # Denominator is the active-cell universe.
    assert den == len(all_active_cells())
    # Numerator counts ONLY the active cell, not the stray one.
    assert num == 1
    # Numerator can never exceed the denominator...
    assert num <= den
    # ...and it must equal what the grid table actually shows.
    assert num == _table_filled(rendered)


def test_filled_count_matches_table_on_normal_grid(tmp_path):
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))
    arc.add(_g("g_00003", ("GATHERING", 2), 3.0))

    rendered = status.render(arc)
    num, den = _headline_filled(rendered.splitlines()[0])
    assert num == 3
    assert num == _table_filled(rendered)
    assert num <= den


def test_qd_score_and_best_ignore_cell_outside_active_grid(tmp_path):
    # Same integrity bug as the filled count, for the other two headline figures:
    # an elite OUTSIDE the active enumeration is excluded from the filled count
    # and the grid table, so its fitness must not inflate the headline qd-score
    # (a sum) or claim "best" (a max) -- otherwise the headline advertises a cell
    # the body never shows.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))            # active
    arc.add(_g("g_00002", ("GATHERING", 2), 4.0))         # active
    arc.add(_g("g_00003", ("LEGACYPROF", 0), 99.0))       # outside enumeration

    header = status.render(arc).splitlines()[0]
    # qd-score sums ONLY the active elites (10 + 4), not the stray 99.
    assert _headline_value(header, "qd-score") == 14.0
    # best is the max over active elites (10), not the inflated stray 99.
    assert _headline_value(header, "best") == 10.0
    # the kernel summary (which the headline previously trusted) DOES include
    # the stray elite -- proving the headline now diverges from it on purpose.
    s = arc.summary()
    assert s["qd_score"] == 113.0 and s["best_fitness"] == 99.0


def test_qd_score_and_best_match_kernel_on_clean_grid(tmp_path):
    # With no stray cells the active-cell figures must still equal the kernel's.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))

    header = status.render(arc).splitlines()[0]
    s = arc.summary()
    assert _headline_value(header, "qd-score") == round(s["qd_score"], 3)
    assert _headline_value(header, "best") == round(s["best_fitness"], 3)


def _elites_section(rendered: str) -> str:
    # Everything from the "elites (lineage ..." header to the end of the report.
    lines = rendered.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("elites ("))
    return "\n".join(lines[start:])


def test_elites_lineage_excludes_cell_outside_active_grid(tmp_path):
    # Same headline-vs-body integrity invariant as the filled/qd-score/best
    # figures, applied to the elites LINEAGE listing: an elite OUTSIDE the active
    # enumeration is excluded from the filled count, the qd-score/best headline,
    # AND the grid table -- so it must not surface in the lineage section either.
    # It is given the HIGHEST fitness so the bug (sorting arc.elites() by fitness)
    # would list it FIRST, contradicting a headline "best" that no longer sees it.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))            # active
    arc.add(_g("g_00002", ("GATHERING", 2), 4.0))         # active
    arc.add(_g("g_00003", ("LEGACYPROF", 0), 99.0))       # outside enumeration

    rendered = status.render(arc)
    section = _elites_section(rendered)

    # The stray (highest-fitness) elite must NOT appear in the lineage listing...
    assert "g_00003" not in section
    assert "99.000" not in section
    # ...while the genuine active elites still do.
    assert "g_00001" in section
    assert "g_00002" in section
    # And the headline's "best" agrees with the lineage top (both exclude 99).
    header = rendered.splitlines()[0]
    assert _headline_value(header, "best") == 10.0


def test_elites_lineage_unchanged_on_clean_grid(tmp_path):
    # No stray cells: every elite is active, so the lineage section lists them all.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))

    section = _elites_section(status.render(arc))
    assert "g_00001" in section and "g_00002" in section


def test_qd_score_and_best_exclude_the_none_fallback_row(tmp_path):
    # RUN-QUALITY integrity: the NONE fallback row is not a real niche (R33) --
    # a failed/wandered agent that banked a high-variance score. Commit 25ad63f
    # set out to keep that volatile NONE-row score out of the headline qd-score
    # (a sum) and best (a max), but the implementation reused the NONE-INCLUSIVE
    # `active` set, so a NONE elite still skewed both figures. Give the NONE-row
    # elite the highest fitness so the regression would crown it "best" and add
    # its score to qd-score; the corrected headline must ignore it.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))            # real niche
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))           # real niche
    arc.add(_g("g_00003", ("NONE", 0), 99.0))             # fallback row, volatile

    header = status.render(arc).splitlines()[0]
    # qd-score sums ONLY the real niches (10 + 20), not the NONE-row 99.
    assert _headline_value(header, "qd-score") == 30.0
    # best is the max over real niches (20), not the inflated NONE-row 99.
    assert _headline_value(header, "best") == 20.0
    # The NONE row is still real grid COVERAGE, so it counts toward filled
    # (denominator stays the full active enumeration -- the figures diverge on
    # purpose: filled = coverage, qd-score/best = quality).
    num, den = _headline_filled(header)
    assert num == 3
    assert den == len(all_active_cells())


def test_qd_score_and_best_unaffected_when_no_none_elite(tmp_path):
    # Without a NONE-row elite the quality figures are identical to the
    # active-cell aggregate -- the NONE exclusion only ever removes a NONE elite,
    # it never disturbs an ordinary grid.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), 10.0))
    arc.add(_g("g_00002", ("COMBAT", 1), 20.0))

    header = status.render(arc).splitlines()[0]
    assert _headline_value(header, "qd-score") == 30.0
    assert _headline_value(header, "best") == 20.0
