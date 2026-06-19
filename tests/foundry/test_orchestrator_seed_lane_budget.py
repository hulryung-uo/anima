"""RunConfig must clamp base `seeds` to the GM lane budget, not just `parallel`.

Regression: __post_init__ reduced `parallel` whenever `parallel*seeds` overran
the lane table but never bounded `seeds` itself. run_eval_multi fans seed k onto
`cfg.lane + k` (k in 0..seeds-1) and the GM resolves the workplace as
`LANE_SPOTS[lane % len(LANE_SPOTS)]` (gm.py). With a single slot the highest
expanded lane is `seeds-1`; once `seeds > LANE_BUDGET` that wraps modulo and two
of the SAME genome's concurrent seed evals co-locate on one fixed-start
workplace — the multi-hour stall the lane budget exists to prevent. The parallel
clamp left this hole open: `--seeds N` with N > LANE_BUDGET wedged even at
parallel=1. (reeval._lane_safe_seeds already guards the same class for reeval.)

We assert that for any seed request the per-slot expanded lane block of the
HIGHEST slot stays strictly inside the GM table — i.e. the lanes never wrap.
"""
from __future__ import annotations

import pytest

from foundry.orchestrator import LANE_BUDGET, RunConfig


def _highest_lane(rc: RunConfig) -> int:
    """Highest GM lane any seed eval can land on for this config.

    Mirrors _eval_cfg: slot s owns lanes [s*span, s*span+span-1], and the worst
    case is the top slot running its full span of seeds.
    """
    span = rc.slot_span
    return (rc.parallel - 1) * span + (span - 1)


def test_seeds_above_budget_is_clamped() -> None:
    rc = RunConfig(parallel=1, seeds=LANE_BUDGET + 5)
    assert rc.seeds == LANE_BUDGET
    # single slot, span == seeds → highest lane == seeds-1 must stay in-table
    assert _highest_lane(rc) < LANE_BUDGET


def test_seeds_far_above_budget_with_parallel_request() -> None:
    # Without the seed clamp, parallel collapses to 1 but span stays huge and
    # the lane wraps. With it, both the clamp and the parallel reduction hold.
    rc = RunConfig(parallel=4, seeds=LANE_BUDGET * 3)
    assert rc.seeds == LANE_BUDGET
    assert rc.parallel == 1
    assert _highest_lane(rc) < LANE_BUDGET


@pytest.mark.parametrize("parallel", [1, 2, 3, 4, 8])
@pytest.mark.parametrize("seeds", [1, 2, 5, LANE_BUDGET, LANE_BUDGET + 1, LANE_BUDGET * 4])
@pytest.mark.parametrize("max_seeds", [None, 2, 4, LANE_BUDGET + 2])
def test_lane_block_never_wraps(parallel: int, seeds: int, max_seeds: int | None) -> None:
    """The invariant the whole budget machinery exists to guarantee: across the
    full param space, the highest seed lane never reaches LANE_BUDGET (so the GM
    modulo never co-locates two seeds of one genome on a single workplace)."""
    rc = RunConfig(parallel=parallel, seeds=seeds, max_seeds=max_seeds)
    assert rc.seeds >= 1
    assert rc.parallel >= 1
    assert _highest_lane(rc) < LANE_BUDGET, (
        f"lane block wraps: parallel={rc.parallel} seeds={rc.seeds} "
        f"span={rc.slot_span} highest_lane={_highest_lane(rc)} >= {LANE_BUDGET}"
    )


def test_seeds_within_budget_untouched() -> None:
    rc = RunConfig(parallel=1, seeds=3)
    assert rc.seeds == 3
    assert rc.parallel == 1
