"""reeval_genome must stride the GM workplace LANE by slot, not just the ports.

Regression: reeval_genome strided proxy_port/web_port by `slot * seeds` but pinned
`lane=0`. run_eval_multi fans each seed out as lane+k / port+k for k in 0..seeds-1,
so every slot's seeds landed on the SAME GM workplace lanes (0..seeds-1). Two
concurrent reeval slots therefore co-located their fixed-start setups on one lane
— exactly the lane-collision class the orchestrator's lane budget prevents — even
though their ports were safely separated. The per-slot lane blocks must be
disjoint, just like the per-slot port blocks already are.
"""
from __future__ import annotations

from foundry import reeval
from foundry.kernel.archive import Genome


class _OkRes:
    ok = True
    score = 1.0
    per_seed_fitness = [1.0]
    cell = ("GATHERING", 0)


def _capture_cfgs(monkeypatch, seeds: int, slots: list[int]):
    """Run reeval_genome for several slots, capturing each EvalConfig built."""
    cfgs = []
    monkeypatch.setattr(reeval, "_prepare_worktree", lambda slot, ref: reeval.__file__)

    def _fake_run(cfg, **_k):
        cfgs.append(cfg)
        return _OkRes()

    monkeypatch.setattr(reeval, "run_eval_multi", _fake_run)

    g = Genome(id="g_00007", code_ref="deadbeef",
               config={"persona": "miner", "fixed_start": "miner"},
               eval={"fitness": 10.0, "cell": ["GATHERING", 0],
                     "per_seed_fitness": [10.0]})
    for slot in slots:
        reeval.reeval_genome(arc=None, g=g, seeds=seeds, window_s=60, slot=slot)
    return cfgs


def test_lane_strides_with_slot_like_ports(monkeypatch):
    seeds = 3
    cfgs = _capture_cfgs(monkeypatch, seeds=seeds, slots=[0, 1, 2])

    # Each slot's lane base must stride by `seeds` so the per-slot lane blocks
    # (run_eval_multi uses lane..lane+seeds-1) never overlap.
    lanes = [c.lane for c in cfgs]
    assert lanes == [0, 3, 6], lanes

    # The lane stride must match the port stride (both reserve a `seeds`-wide
    # block per slot) — the bug was that ports strided while lane did not.
    proxy_step = cfgs[1].proxy_port - cfgs[0].proxy_port
    lane_step = cfgs[1].lane - cfgs[0].lane
    assert lane_step == proxy_step == seeds


def test_concurrent_slot_lane_blocks_are_disjoint(monkeypatch):
    # run_eval_multi expands a seed k onto cfg.lane + k. Two adjacent slots must
    # not share any expanded lane, or their fixed-start GM setups collide.
    seeds = 4
    cfgs = _capture_cfgs(monkeypatch, seeds=seeds, slots=[0, 1])

    def _expanded_lanes(cfg):
        return {cfg.lane + k for k in range(seeds)}

    block0 = _expanded_lanes(cfgs[0])
    block1 = _expanded_lanes(cfgs[1])
    assert block0.isdisjoint(block1), (block0, block1)


def test_single_slot_starts_at_lane_zero(monkeypatch):
    # The common (sequential) path is unchanged: slot 0 still starts at lane 0.
    cfgs = _capture_cfgs(monkeypatch, seeds=2, slots=[0])
    assert cfgs[0].lane == 0
