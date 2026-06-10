"""score.py — parse + fitness + descriptor for a trajectory (dev convenience).

Mutable-side tool: composes the kernel's parser, fitness oracle, and descriptor
into a one-shot CLI for inspecting what a trajectory scores. Useful for
calibrating weights/bins and for the develop-cycle's observe step.

    python3 -m foundry.score data/trajectories/<file>.jsonl
    python3 -m foundry.score data/trajectories/*.jsonl   # batch
"""

from __future__ import annotations

import sys

from foundry.kernel.descriptor import compute_descriptor
from foundry.kernel.fitness import compute_fitness
from foundry.kernel.trajectory import parse_file


def score_one(path: str) -> None:
    summ = parse_file(path)
    fit = compute_fitness(summ)
    desc = compute_descriptor(summ)

    name = path.rsplit("/", 1)[-1]
    print(f"\n=== {name} ===")
    print(f"  duration   {summ.duration_s:.0f}s ({summ.duration_h:.3f}h)  "
          f"lines={summ.line_count} errors={summ.parse_errors}")
    print(f"  FITNESS    {fit.total:.3f}")
    print(f"    gate     {fit.viability_gate:.3f}  "
          f"(alive={fit.alive_fraction:.2f} live={fit.liveness:.2f} loop={fit.loop_penalty:.2f})")
    print(f"    terms    skill={fit.skill_term:.3f} worth={fit.worth_term:.3f} "
          f"produce={fit.produce_term:.3f} behavior={fit.behavior_bonus:.3f}")
    print(f"    rates    skill={fit.skill_gain_rate:.2f}/h gold={fit.networth_rate:.1f}/h "
          f"regions={fit.regions_rate:.1f}/h")
    print(f"  CELL       {desc.cell}   ({desc.label()})")
    print(f"    axes     prof={desc.profession_focus} "
          f"soc={desc.sociability:.3f}[{desc.sociability_bin}] "
          f"aggr={desc.aggression:.3f}[{desc.aggression_bin}] "
          f"mob={desc.mobility_rate:.1f}/h[{desc.mobility_bin}]")
    if desc.profession_gains:
        print(f"    gains    {desc.profession_gains}")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python3 -m foundry.score <trajectory.jsonl> [more.jsonl ...]")
        return 2
    for path in argv:
        try:
            score_one(path)
        except FileNotFoundError:
            print(f"  (not found: {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
