"""status.py headline filled-cell count must match the cells the table renders.

The kernel's summary()["filled_cells"] is len(grid). The grid can hold a cell
whose profession/sociability is NOT in the active enumeration (a profession
dropped or renamed across an evolution). Such a cell inflates the headline
numerator -- potentially past the denominator -- yet is silently absent from the
grid table render() prints below it, so the report contradicts itself. render()
must count filled cells against the SAME active-cell universe it tabulates.
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
