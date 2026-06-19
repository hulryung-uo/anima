"""confirm-on-promotion must not pool a re-run that drifted to a DIFFERENT cell.

merge_confirmation (kernel) keeps the first round's descriptor/cell but pools
per_seed_fitness from both rounds unconditionally. confirm-on-promotion targets
would-be winners, which cluster in the high-variance professions whose
descriptor is most prone to drift to a neighbouring sociability bin on a re-run.
Pooling a drifted round folds off-cell seeds into the cell's reliability bound
(mean − λ·pstdev) — the number the kernel promotes on. _pool_confirmation gates
the merge on cell equality so off-cell corroboration is dropped, exactly like a
failed confirm. This test pins that gate; it does NOT touch the kernel merge or
the held-out / min(fitness,reliability) semantics.
"""
from __future__ import annotations

from foundry.kernel.descriptor import Descriptor
from foundry.kernel.eval import EvalResult
from foundry.kernel.fitness import FitnessBreakdown
from foundry.orchestrator import _pool_confirmation


def _result(per_seed: list[float], prof: str, soc_bin: int) -> EvalResult:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return EvalResult(
        ok=True,
        fitness=FitnessBreakdown(total=mean),
        descriptor=Descriptor(profession_focus=prof, sociability_bin=soc_bin),
        per_seed_fitness=list(per_seed),
    )


def test_same_cell_pools_both_rounds():
    first = _result([10.0, 10.0], "COMBAT", 2)
    confirm = _result([4.0, 4.0], "COMBAT", 2)
    out = _pool_confirmation(first, confirm)
    # pooled per-seed evidence spans both rounds; mean falls to the honest 7.0
    assert out.per_seed_fitness == [10.0, 10.0, 4.0, 4.0]
    assert out.score == 7.0
    assert tuple(out.cell) == ("COMBAT", 2)


def test_drifted_cell_is_not_pooled():
    # The re-run scored just as high but BEHAVED like a different cell
    # (sociability bin 1 not 2). Its seeds are not evidence for COMBAT|2.
    first = _result([10.0, 10.0], "COMBAT", 2)
    confirm = _result([10.0, 10.0], "COMBAT", 1)
    out = _pool_confirmation(first, confirm)
    assert out is first, "off-cell confirm must be dropped, not pooled"
    assert out.per_seed_fitness == [10.0, 10.0]
    assert tuple(out.cell) == ("COMBAT", 2)


def test_profession_drift_is_not_pooled():
    first = _result([10.0, 10.0], "COMBAT", 2)
    confirm = _result([99.0, 99.0], "GATHERING", 2)  # whole profession drifted
    out = _pool_confirmation(first, confirm)
    assert out is first
    assert out.per_seed_fitness == [10.0, 10.0]


def test_failed_confirm_is_dropped():
    first = _result([10.0, 10.0], "COMBAT", 2)
    confirm = EvalResult(ok=False, error="all seeds failed")
    out = _pool_confirmation(first, confirm)
    assert out is first
    assert out.per_seed_fitness == [10.0, 10.0]


def test_drift_does_not_lower_reliability_via_off_cell_seeds():
    # Concretely: a steady first round; a drifted re-run that, if pooled, would
    # inject variance the cell never exhibited. The dropped-confirm result keeps
    # the first round's reliability intact.
    from foundry.kernel.archive import reliability_score
    first = _result([20.0, 20.0, 20.0], "CRAFTING", 0)
    drifted = _result([0.0, 0.0, 0.0], "CRAFTING", 1)
    out = _pool_confirmation(first, drifted)
    rel = reliability_score(out.per_seed_fitness, out.score)
    # unpooled: pstdev 0 → reliability == mean 20.0 (no off-cell variance hit)
    assert rel == 20.0
