"""A develop cycle is framed as EXPLORE only when its suggested cell is empty.

``select.suggest_target_cell`` returns an EMPTY cell while the grid has frontier,
but once the grid is FULL it deliberately returns a *filled* headroom cell so the
parent can be row-matched (its docstring). ``mutate_with_claude`` keys EXPLORE vs
IMPROVE purely off ``target_cell is not None`` and its EXPLORE prompt says "land
in the empty cell `X`" — so forwarding a filled cell told the LLM to colonise
ground that is already occupied and mislabelled a pure in-cell IMPROVE as an
exploration. ``_explore_target`` keeps the full suggested cell for parent pairing
but only hands the mutator (and records as the cycle target) a genuinely empty
cell. These tests pin that distinction without touching the kernel.
"""
from __future__ import annotations

from foundry.kernel.archive import Archive, Genome
from foundry.orchestrator import _explore_target
from foundry.select import empty_cells, targetable_cells


def _genome(gid: str, cell: tuple, fit: float) -> Genome:
    return Genome(
        id=gid,
        code_ref="deadbeef",
        eval={"fitness": fit, "cell": list(cell),
              "per_seed_fitness": [fit]},
    )


def _fill_all_but(arc: Archive, leave_empty: set[tuple]) -> None:
    """Promote a trivial elite into every targetable cell except `leave_empty`."""
    n = 0
    for cell in targetable_cells():
        if tuple(cell) in {tuple(c) for c in leave_empty}:
            continue
        n += 1
        arc.add(_genome(f"g_{n:05d}", cell, 10.0 + n))


def test_empty_suggested_cell_is_an_explore_target(tmp_path):
    arc = Archive(tmp_path / "archive")
    # Pick one real empty cell and fill the rest, so it stays in empty_cells().
    empty_cell = targetable_cells()[0]
    _fill_all_but(arc, leave_empty={empty_cell})
    assert tuple(empty_cell) in {tuple(c) for c in empty_cells(arc)}

    # An empty suggested cell is forwarded verbatim → EXPLORE framing downstream.
    assert _explore_target(arc, empty_cell) == empty_cell


def test_filled_headroom_cell_is_not_an_explore_target(tmp_path):
    arc = Archive(tmp_path / "archive")
    # FULL grid: every targetable cell occupied → suggest_target_cell returns a
    # FILLED headroom cell, which must NOT be framed as "land in the empty cell".
    _fill_all_but(arc, leave_empty=set())
    assert empty_cells(arc) == []

    filled_cell = targetable_cells()[0]
    assert tuple(filled_cell) in {tuple(c) for c in arc.grid_cells()} \
        if hasattr(arc, "grid_cells") else filled_cell in [g.cell for g in arc.elites()]
    # A filled cell collapses to None → IMPROVE framing against the parent's cell.
    assert _explore_target(arc, filled_cell) is None


def test_none_suggestion_stays_none(tmp_path):
    arc = Archive(tmp_path / "archive")
    assert _explore_target(arc, None) is None


def test_explore_target_membership_is_tuple_robust(tmp_path):
    # suggest_target_cell yields tuples, but be robust if a list-shaped cell is
    # passed (config round-trips cells through JSON lists): membership must still
    # match the empty set by value, not by container identity.
    arc = Archive(tmp_path / "archive")
    empty_cell = targetable_cells()[0]
    _fill_all_but(arc, leave_empty={empty_cell})
    as_list = list(empty_cell)
    out = _explore_target(arc, as_list)
    assert out is as_list  # genuinely empty → forwarded unchanged


def test_filled_cell_with_one_remaining_empty_still_collapses(tmp_path):
    # Grid has frontier (one empty cell), but the SUGGESTED cell here is a
    # different, already-filled one — it must still collapse to None.
    arc = Archive(tmp_path / "archive")
    cells = targetable_cells()
    empty_cell = cells[0]
    _fill_all_but(arc, leave_empty={empty_cell})
    a_filled_cell = cells[1]
    assert a_filled_cell != empty_cell
    assert _explore_target(arc, a_filled_cell) is None
    assert _explore_target(arc, empty_cell) == empty_cell
