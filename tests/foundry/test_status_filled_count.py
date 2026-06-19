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
