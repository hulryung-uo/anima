"""choose_parent_for_target: the code-recency tiebreak must not eclipse quality.

The same-row parent picker multiplies a quality factor (1.0 + 0.1·quality) by a
code-recency factor that scales with genome-id age rank. The recency factor is
meant to be a MILD tiebreak (prefer the fresher lineage among near-equals) — not
a dominating signal. With the old ×3 ceiling (coefficient 2.0) the NEWEST row
member won the draw even when it was several-fold worse than a steadier elite
beside it, which silently undoes the variance-aware / held-out-correction
guarantee that _selection_quality (= min(fitness, reliability)) provides.

These tests assert the corrected behaviour:
  * a clearly-better steady elite outweighs a much-worse NEWEST same-row elite, and
  * among genomes of equal quality the fresher one is still (mildly) preferred.
"""
from foundry.kernel.archive import Genome
from foundry.select import _selection_quality, choose_parent_for_target


def _mk(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": per_seed,
        },
    )


class _FakeArchive:
    """Minimal Archive double exposing elites()/get_elite() over a fixed grid."""

    def __init__(self, elites: list[Genome]) -> None:
        self._by_cell = {tuple(g.cell): g for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._by_cell.values())

    def get_elite(self, cell: tuple):
        return self._by_cell.get(tuple(cell))


def _row_weights(row: list[Genome], coef: float) -> dict[str, float]:
    """Reproduce the choose_parent_for_target same-row weight formula."""
    by_age = sorted(row, key=lambda g: g.id)
    rank = {g.id: i for i, g in enumerate(by_age)}
    n = len(row)
    out: dict[str, float] = {}
    for g in row:
        qf = 1.0 + 0.1 * max(0.0, _selection_quality(g))
        rf = (1.0 + coef * (rank[g.id] / (n - 1)) if n > 1 else 1.0)
        out[g.id] = qf * rf
    return out


def test_recency_does_not_outvote_a_clearly_better_steady_elite():
    # Realistic COMBAT row (fitness band ~5-25): a steady, 4x-better OLDER elite
    # vs a weak NEWEST one. Selection probability must favour the better elite.
    good = _mk("g_00010", ("COMBAT", 1), [23.0, 24.0, 25.0])   # fitness 24
    weak = _mk("g_00200", ("COMBAT", 0), [5.0, 6.0, 7.0])      # fitness 6, newest
    assert _selection_quality(good) > _selection_quality(weak)

    w = _row_weights([good, weak], coef=0.5)
    total = sum(w.values())
    # The 4x-better steady elite must be more likely than the newer weak one.
    assert w["g_00010"] > w["g_00200"]
    assert w["g_00010"] / total > 0.5

    # Regression guard: under the OLD ×3 ceiling (coef 2.0) the weak newest
    # genome wrongly dominated — proving the bug this fix removes.
    old = _row_weights([good, weak], coef=2.0)
    assert old["g_00200"] > old["g_00010"]


def test_recency_still_breaks_ties_toward_the_fresher_lineage():
    # Equal-quality elites: recency should still (mildly) prefer the newer one,
    # so the tiebreak intent is preserved.
    older = _mk("g_00010", ("COMBAT", 1), [24.0, 24.0, 24.0])
    newer = _mk("g_00200", ("COMBAT", 0), [24.0, 24.0, 24.0])
    assert _selection_quality(older) == _selection_quality(newer)

    w = _row_weights([older, newer], coef=0.5)
    assert w["g_00200"] > w["g_00010"]


def test_choose_parent_for_target_prefers_better_steady_elite_in_distribution():
    # End-to-end through the real function: across many seeds the better steady
    # elite is drawn the majority of the time, NOT the newer weak one.
    good = _mk("g_00010", ("COMBAT", 1), [23.0, 24.0, 25.0])
    weak = _mk("g_00200", ("COMBAT", 0), [5.0, 6.0, 7.0])
    arc = _FakeArchive([good, weak])

    counts = {"g_00010": 0, "g_00200": 0}
    for seed in range(400):
        pick = choose_parent_for_target(arc, ("COMBAT", 0), seed=seed)
        counts[pick.id] += 1

    assert counts["g_00010"] > counts["g_00200"], counts


def test_single_member_row_has_no_division_by_zero():
    # n == 1 takes the `else 1.0` branch — must not touch (n - 1).
    only = _mk("g_00010", ("COMBAT", 1), [10.0, 10.0, 10.0])
    arc = _FakeArchive([only])
    assert choose_parent_for_target(arc, ("COMBAT", 1), seed=0) is only
