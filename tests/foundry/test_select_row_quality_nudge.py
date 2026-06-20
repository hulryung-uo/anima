"""suggest_target_cell's empty-cell row-quality nudge must stay STRICTLY monotone.

When the grid still has frontier, ``suggest_target_cell`` weights each empty cell
toward PROVEN rows: the primary signal is ``_PROVEN_STEP * row_filled`` (more
covered cells = more working machinery), with the row's BEST quality as a
secondary "this loop also scores well" nudge (the docstring: "higher best fitness
in the row").

The old nudge was ``min(2.0, max(0.0, q) / 20.0)`` — a linear ramp that saturates
at its 2.0 cap for any quality >= 40. Live selection-quality reaches ~235, so
EVERY proven row hit the ceiling and the "higher best fitness in the row"
preference was silently defeated: a q=50 row and a q=235 row received the
identical bonus. The fix replaces it with the file's own bounded, strictly
monotone curve (``q/(q+half)``), ceilinged BELOW one ``_PROVEN_STEP`` so a more
quality-rich proven row is genuinely preferred while a more-COVERED row still
always wins.
"""
from foundry.kernel.archive import Genome
from foundry.select import (
    _PROVEN_STEP,
    _ROW_QUALITY_MAX,
    _row_quality_bonus,
    suggest_target_cell,
)


def _mk(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": per_seed,
        },
    )


def test_row_quality_bonus_is_strictly_monotone_past_the_old_saturation():
    # The old ramp saturated flat at q >= 40 (min(2.0, q/20) == 2.0 for both).
    # A q=235 proven row MUST now outweigh a q=50 one — the whole point.
    assert _row_quality_bonus(50.0) < _row_quality_bonus(235.0)
    # Also strictly increasing across the range, not just at the extremes.
    qs = [0.0, 10.0, 30.0, 40.0, 50.0, 100.0, 200.0, 235.0]
    bonuses = [_row_quality_bonus(q) for q in qs]
    assert all(a < b for a, b in zip(bonuses, bonuses[1:]))


def test_row_quality_bonus_bounded_below_one_proven_step():
    # The secondary nudge must NEVER outvote one extra filled cell (the primary
    # "proven machinery" signal): bounded strictly below _PROVEN_STEP, and a
    # negative (high-variance) row quality contributes no bonus.
    assert _row_quality_bonus(1e12) < _ROW_QUALITY_MAX < _PROVEN_STEP
    assert _row_quality_bonus(-5.0) == 0.0
    assert _row_quality_bonus(0.0) == 0.0


def test_quality_breaks_the_tie_between_two_equally_covered_proven_rows():
    # Two professions, each with ONE filled cell (equal row_filled == 1) and one
    # empty cell. GATHERING's elite is far higher quality than COMBAT's; both are
    # well above the old saturation point (40). With row_filled equal, the empty
    # cell of the HIGHER-quality row must carry strictly more weight — under the
    # old saturating ramp the two weights were identical and the tie was a
    # coin-flip on seed alone.
    g_gather = _mk("g_a", ("GATHERING", 0), [200.0, 220.0, 240.0])  # q ~ huge
    g_combat = _mk("g_b", ("COMBAT", 0), [50.0, 55.0, 45.0])        # q >> 40 too

    from foundry.select import _row_best_quality, _selection_quality

    elites = [g_gather, g_combat]
    row_best = _row_best_quality(elites)
    # Both rows clear the old 40-saturation, so the OLD ramp would tie them.
    assert _selection_quality(g_gather) > 40.0
    assert _selection_quality(g_combat) > 40.0

    # Mirror suggest_target_cell's empty-branch weight for an empty cell whose
    # row has exactly one filled cell.
    def _empty_weight(prof: str) -> float:
        return (1.0
                + _PROVEN_STEP * 1
                + _row_quality_bonus(row_best[prof]))

    w_gather = _empty_weight("GATHERING")
    w_combat = _empty_weight("COMBAT")
    assert w_gather > w_combat                     # higher-quality row preferred
    # ...but the quality gap is still a SECONDARY nudge: it is smaller than the
    # bonus a single extra filled cell (one _PROVEN_STEP) would add.
    assert (w_gather - w_combat) < _PROVEN_STEP


def test_more_covered_row_still_beats_a_higher_quality_thinner_row():
    # Primary signal dominates: a 2-cell COMBAT row (row_filled == 2) of MODEST
    # quality must out-weight a 1-cell GATHERING row of MAX quality for an empty
    # cell — "more proven machinery" wins over "scores a bit better".
    combat0 = _mk("g_c0", ("COMBAT", 0), [30.0, 30.0, 30.0])
    combat1 = _mk("g_c1", ("COMBAT", 1), [30.0, 30.0, 30.0])
    gather0 = _mk("g_g0", ("GATHERING", 0), [1000.0, 1000.0, 1000.0])

    from foundry.select import _row_best_quality

    row_best = _row_best_quality([combat0, combat1, gather0])
    w_combat_empty = 1.0 + _PROVEN_STEP * 2 + _row_quality_bonus(row_best["COMBAT"])
    w_gather_empty = 1.0 + _PROVEN_STEP * 1 + _row_quality_bonus(row_best["GATHERING"])
    assert w_combat_empty > w_gather_empty


def test_suggest_target_cell_runs_and_returns_an_empty_targetable_cell():
    # End-to-end smoke: with frontier present, the empty-branch path executes and
    # returns a genuinely empty, targetable cell (never raises on the new term).
    class _Arc:
        def __init__(self, elites):
            self._by_cell = {tuple(g.cell): g for g in elites}

        def elites(self):
            return list(self._by_cell.values())

        def get_elite(self, cell):
            return self._by_cell.get(tuple(cell))

        @property
        def grid(self):
            from foundry.kernel.archive import cell_to_str
            return {cell_to_str(c): g.id for c, g in self._by_cell.items()}

    arc = _Arc([_mk("g_a", ("GATHERING", 0), [200.0, 220.0, 240.0])])
    from foundry.select import empty_cells

    out = suggest_target_cell(arc, seed=0)
    assert out is not None
    assert tuple(out) in {tuple(c) for c in empty_cells(arc)}
