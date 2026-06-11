"""Held-out re-eval of archived genomes (FOUNDRY.md §9.6) — report-only.

Re-runs a genome's exact code (its pinned commit, in a slot worktree) under
its recorded persona/fixed-start on FRESH accounts, and reports how well the
archived fitness replicates. It deliberately does NOT touch the grid — demoting
a champion that fails to replicate is a human decision; this tool produces the
evidence.

    python3 -m foundry.reeval g_00013            # 3 fresh seeds, 600s
    python3 -m foundry.reeval g_00026 --seeds 5 --window 600
    python3 -m foundry.reeval --elites            # re-check every grid elite

Mutator-editable (it only reads the kernel and runs evals through it).
"""

from __future__ import annotations

import statistics

from foundry.kernel.archive import Archive, Genome
from foundry.kernel.eval import EvalConfig, run_eval_multi
from foundry.orchestrator import _prepare_worktree


def reeval_genome(arc: Archive, g: Genome, seeds: int, window_s: int,
                  slot: int = 0) -> dict:
    cfg_src = g.config or {}
    persona = cfg_src.get("persona", "miner")
    fixed_start = cfg_src.get("fixed_start", "miner")
    wt = _prepare_worktree(slot, g.code_ref or "HEAD")

    cfg = EvalConfig(
        account_user=f"re{g.id.replace('g_', '')}",
        persona=persona,
        fixed_start=fixed_start,
        window_s=window_s,
        proxy_port=2680 + slot * max(1, seeds),
        web_port=8200 + slot * max(1, seeds),
        lane=0,
        repo_root=wt,
    )
    res = run_eval_multi(cfg, seeds=seeds)
    out = {
        "genome": g.id,
        "cell": g.cell,
        "recorded": g.fitness,
        "recorded_seeds": g.eval.get("per_seed_fitness", []),
        "ok": res.ok,
    }
    if res.ok:
        out["held_out"] = res.score
        out["held_out_seeds"] = res.per_seed_fitness
        out["held_out_cell"] = list(res.cell)
        out["ratio"] = res.score / g.fitness if g.fitness else float("inf")
    else:
        out["error"] = res.error
    return out


def _verdict(r: dict) -> str:
    if not r["ok"]:
        return f"EVAL FAILED: {r.get('error')}"
    notes = []
    if r["ratio"] < 0.5:
        notes.append("DOES NOT REPLICATE (held-out < 50% of recorded) — consider demotion")
    elif r["ratio"] < 0.8:
        notes.append("weak replication (50-80%)")
    else:
        notes.append("replicates")
    if list(r["held_out_cell"]) != list(r["cell"]):
        notes.append(f"CELL DRIFT: archived {tuple(r['cell'])} vs held-out "
                     f"{tuple(r['held_out_cell'])}")
    return "; ".join(notes)


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Held-out re-eval (report-only)")
    ap.add_argument("genomes", nargs="*", help="genome ids (g_00013 …)")
    ap.add_argument("--elites", action="store_true", help="re-check every grid elite")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--window", type=int, default=600)
    ap.add_argument("--archive", default="foundry/archive")
    args = ap.parse_args(argv)

    arc = Archive(args.archive)
    targets: list[Genome] = []
    if args.elites:
        targets = sorted(arc.elites(), key=lambda g: -g.fitness)
    for gid in args.genomes:
        g = arc.get(gid)
        if g is None:
            print(f"{gid}: not found")
            continue
        targets.append(g)
    if not targets:
        print("nothing to re-eval (pass genome ids or --elites)")
        return 2

    ratios = []
    for g in targets:
        print(f"[reeval] {g.id} cell={g.cell} recorded={g.fitness:.3f} "
              f"persona={g.config.get('persona')}/{g.config.get('fixed_start')} …")
        r = reeval_genome(arc, g, seeds=args.seeds, window_s=args.window)
        if r["ok"]:
            print(f"[reeval] {g.id}: held-out {r['held_out']:.3f} "
                  f"(seeds {[round(v, 2) for v in r['held_out_seeds']]}) "
                  f"ratio {r['ratio']:.2f} → {_verdict(r)}")
            ratios.append(r["ratio"])
        else:
            print(f"[reeval] {g.id}: {_verdict(r)}")

    if len(ratios) > 1:
        print(f"\n[reeval] median replication ratio: {statistics.median(ratios):.2f} "
              f"over {len(ratios)} genomes")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
