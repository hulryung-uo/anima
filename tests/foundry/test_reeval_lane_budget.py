"""reeval must clamp --seeds to the GM lane budget, like the orchestrator does.

Regression: reeval_genome strided the per-slot lane block by `slot * seeds`
(test_reeval_lane_stride) but never bounded the block against the GM table size.
run_eval_multi fans a seed k onto ``cfg.lane + k`` and the GM resolves the
workplace as ``LANE_SPOTS[lane % len(LANE_SPOTS)]``: once the highest expanded
lane ``(slot+1)*seeds - 1`` reaches LANE_BUDGET it wraps modulo and two of the
SAME genome's concurrent seed evals collide on one fixed-start workplace — the
multi-hour stall the orchestrator's lane-budget clamp exists to prevent. reeval
now applies the same clamp.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Genome


class _OkRes:
    ok = True
    score = 1.0
    per_seed_fitness = [1.0]
    cell = ("GATHERING", 0)


def _run(monkeypatch, seeds: int, slot: int):
    """Run reeval_genome capturing the EvalConfig and the seeds actually run."""
    captured: dict = {}
    monkeypatch.setattr(reeval, "_prepare_worktree", lambda s, ref: reeval.__file__)

    def _fake_run(cfg, **kw):
        captured["cfg"] = cfg
        captured["seeds"] = kw.get("seeds")
        return _OkRes()

    monkeypatch.setattr(reeval, "run_eval_multi", _fake_run)
    g = Genome(id="g_00007", code_ref="deadbeef",
               config={"persona": "miner", "fixed_start": "miner"},
               eval={"fitness": 10.0, "cell": ["GATHERING", 0],
                     "per_seed_fitness": [10.0]})
    reeval.reeval_genome(arc=None, g=g, seeds=seeds, window_s=60, slot=slot)
    return captured


def _expanded_lanes(cfg, seeds: int) -> set[int]:
    # run_eval_multi expands seed k onto cfg.lane + k.
    return {cfg.lane + k for k in range(seeds)}


def test_oversized_seeds_clamped_to_budget_at_slot0(monkeypatch):
    # More seeds than the whole table: every expanded lane must stay < LANE_BUDGET
    # so none wrap (LANE_SPOTS[lane % len]) onto an already-used workplace.
    cap = _run(monkeypatch, seeds=reeval.LANE_BUDGET + 5, slot=0)
    assert cap["seeds"] == reeval.LANE_BUDGET
    lanes = _expanded_lanes(cap["cfg"], cap["seeds"])
    assert max(lanes) < reeval.LANE_BUDGET


def test_higher_slot_gets_a_smaller_clamp(monkeypatch):
    # slot s reserves lanes s*seeds..(s+1)*seeds-1, so the per-slot ceiling is
    # LANE_BUDGET // (s+1). At slot 1 the block must end below LANE_BUDGET.
    cap = _run(monkeypatch, seeds=reeval.LANE_BUDGET, slot=1)
    assert cap["seeds"] == reeval.LANE_BUDGET // 2
    lanes = _expanded_lanes(cap["cfg"], cap["seeds"])
    assert max(lanes) < reeval.LANE_BUDGET
    assert min(lanes) >= cap["cfg"].lane  # block starts at its strided base


def test_in_budget_seeds_are_untouched(monkeypatch):
    # A request that already fits passes through unchanged (no spurious clamp).
    fits = max(1, reeval.LANE_BUDGET // 2)
    cap = _run(monkeypatch, seeds=fits, slot=0)
    assert cap["seeds"] == fits
    assert cap["cfg"].lane == 0


def test_clamp_never_drops_below_one(monkeypatch):
    # Pathological slot far past the table still yields a runnable single seed,
    # never 0 (which run_eval_multi would treat as a no-seed eval).
    cap = _run(monkeypatch, seeds=4, slot=reeval.LANE_BUDGET + 3)
    assert cap["seeds"] == 1


def test_lane_safe_seeds_pure_function():
    B = reeval.LANE_BUDGET
    assert reeval._lane_safe_seeds(B + 5, 0) == B
    assert reeval._lane_safe_seeds(B, 1) == B // 2
    assert reeval._lane_safe_seeds(2, 0) == 2          # already fits
    assert reeval._lane_safe_seeds(4, B + 3) == 1      # floored at 1
