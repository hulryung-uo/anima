"""Develop-cycle orchestrator (FOUNDRY.md §6, §7) — mutator-editable.

Phase-1 PARALLEL evolution loop: K slot worktrees × K ServUO accounts run
develop cycles concurrently. Each cycle:

    select parent (frontier-biased)              foundry/select.py
    checkout parent code into a slot worktree    (variants never touch main HEAD)
    mutate anima/ into a variant                 foundry/mutate.py  (claude | noop)
    revert foundry/kernel to the pin             foundry/kernel/safety.py
    eval the variant live (fixed-start, GM)      foundry/kernel/eval.py
    insert into the MAP-Elites grid              foundry/kernel/archive.py  (main thread)

Anti-gaming/integrity invariants owned here:
  - the kernel that SCORES is the one imported by THIS process from the main
    repo — a mutated worktree kernel never runs; the pin-revert keeps variant
    lineage clean too.
  - uo_proxy (the trajectory recorder) always runs from the main repo.
  - archive inserts happen single-writer in the main thread; each archived
    genome's commit is pinned under refs/foundry/<id> so it stays reachable.

Safety: STOP file halts scheduling; a hard genome cap backstops runaway loops;
the GM session lock serializes fixed-start setups.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from foundry import mutate, observe, select
from foundry.kernel import safety
from foundry.kernel.archive import Archive, Genome
from foundry.kernel.eval import ANIMA_ROOT, EvalConfig, EvalResult, run_eval_multi

REPO = str(ANIMA_ROOT)
WORKTREES = ANIMA_ROOT / ".worktrees"


@dataclass
class RunConfig:
    n_cycles: int = 1
    window_s: int = 120
    persona: str = "miner"
    backend: str = "noop"               # "claude" | "noop"
    parallel: int = 1                   # K slots (≤ safety.MAX_CONCURRENT_EVALS)
    seeds: int = 1                      # re-runs per genome, averaged
    fixed_start: str = "miner"          # kernel gm profile; "" = raw spawn
    mutate_model: str = "sonnet"
    account_prefix: str = "evo"
    base_proxy_port: int = 2630
    base_web_port: int = 8170
    archive_root: str = "foundry/archive"
    mutate_timeout: int = 1500          # EXPLORE mutations + worktree pytest run long
    run_id: str = ""                    # account-name nonce; default = time-based
    force_seed: bool = False            # plant a HEAD-based root genome even if grid non-empty

    def __post_init__(self) -> None:
        self.parallel = max(1, min(self.parallel, safety.MAX_CONCURRENT_EVALS))
        if not self.run_id:
            self.run_id = format(int(time.time()) % 1679616, "04x")  # 4 hex chars


# --- git helpers -----------------------------------------------------------

def _git(repo: str | Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=timeout)


def _prepare_worktree(slot: int, ref: str) -> Path:
    """Slot worktree checked out (detached) at `ref`, clean of prior leftovers."""
    wt = WORKTREES / f"slot{slot}"
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = _git(REPO, "worktree", "add", "--detach", str(wt), ref)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
    else:
        r = _git(wt, "reset", "--hard", ref)
        if r.returncode != 0:
            raise RuntimeError(f"worktree reset failed: {r.stderr.strip()}")
        # drop untracked junk from earlier cycles, but keep the costly venv
        # and the agent's data dir (it is recreated anyway).
        _git(wt, "clean", "-fdq", "-e", ".venv", "-e", "data")
    return wt


def _pin_ref(gid: str, sha: str) -> None:
    """Keep an archived genome's commit reachable after worktrees move on."""
    if sha:
        _git(REPO, "update-ref", f"refs/foundry/{gid}", sha)


# --- genome assembly --------------------------------------------------------

def _genome_from(arc: Archive, res: EvalResult, parent: str | None,
                 code_ref: str, hypothesis: str, rc: RunConfig,
                 target_cell: tuple | None = None,
                 persona: str | None = None,
                 fixed_start: str | None = None) -> Genome:
    d = res.descriptor
    f = res.fitness
    return Genome(
        id=arc.next_id(),
        parent=parent,
        code_ref=code_ref,
        config={"persona": persona if persona is not None else rc.persona,
                "fixed_start": fixed_start if fixed_start is not None else rc.fixed_start,
                "window_s": rc.window_s, "seeds": rc.seeds,
                "target_cell": list(target_cell) if target_cell else None},
        eval={
            "fitness": f.total if f else 0.0,
            "cell": list(d.cell) if d else [],
            "descriptor": {
                "profession": d.profession_focus if d else "NONE",
                "sociability_bin": d.sociability_bin if d else 0,
                "label": d.label() if d else "",
            },
            "ok": res.ok,
            "per_seed_fitness": res.per_seed_fitness,
            "trajectory_ref": res.trajectory_path,
            "window_start": res.window_start,
            "breakdown": f.as_dict() if f else {},
            "setup": res.setup,
            # descriptor anatomy — lets the mutator see WHY a sociability bin
            # was missed (e.g. 651 moves drowning 27 speeches → 0.038)
            "action_counts": dict(res.summary.action_counts) if res.summary else {},
            "sociability_raw": round(d.sociability, 4) if d else 0.0,
        },
        hypothesis=hypothesis,
        ts=time.time(),
    )


# Which (persona, fixed_start) actually exercises a profession during eval.
# A cycle aiming at BARD-SOCIAL must be evaluated as a bard — under the run's
# default miner the mutated bard loop never even executes (observed twice:
# mage-run cycle 1 landed NONE, main-run cycles 5/6 tuned practice_music that
# a miner eval would never call). EXPLORE cycles map from the target cell,
# IMPROVE cycles from the parent's own cell; NONE falls back to run defaults.
PROFESSION_SETUP: dict[str, tuple[str, str]] = {
    "GATHERING": ("miner", "miner"),
    "MAGIC": ("mage", "mage"),
    "THIEF-STEALTH": ("thief", "thief"),
    "BARD-SOCIAL": ("bard", "bard"),
    "COMBAT": ("adventurer", "warrior"),
    "CRAFTING": ("blacksmith", "crafter"),
}


def _setup_for(rc: RunConfig, parent: Genome | None,
               target: tuple | None) -> tuple[str, str]:
    profession = None
    if target:
        profession = target[0]
    elif parent and parent.cell:
        profession = parent.cell[0]
    return PROFESSION_SETUP.get(profession, (rc.persona, rc.fixed_start))


def _eval_cfg(rc: RunConfig, user: str, slot: int, repo_root: Path | None,
              persona: str | None = None, fixed_start: str | None = None) -> EvalConfig:
    return EvalConfig(
        account_user=user,
        persona=persona if persona is not None else rc.persona,
        window_s=rc.window_s,
        proxy_port=rc.base_proxy_port + slot,
        web_port=rc.base_web_port + slot,
        seed=slot,
        fixed_start=fixed_start if fixed_start is not None else rc.fixed_start,
        repo_root=repo_root,
    )


def _parent_observation(arc: Archive, parent: Genome | None) -> str:
    """Best-effort observation text for the mutate prompt from stored data."""
    if parent is None:
        return "(no parent — this is the first variant)"
    bd = parent.eval.get("breakdown", {})
    desc = parent.eval.get("descriptor", {})
    counts = parent.eval.get("action_counts") or {}
    soc_raw = parent.eval.get("sociability_raw")
    anatomy = ""
    if counts:
        total = sum(counts.values()) or 1
        anatomy = (f"- action mix: {counts} (total {total}); "
                   f"sociability = speech/total = {soc_raw} "
                   f"(bins: <0.02 low, <0.10 mid, ≥0.10 high). To raise it, "
                   f"shrinking the denominator (fewer moves) works as well as "
                   f"more speech.\n")
    return (
        f"# Parent {parent.id} (cell {parent.cell})\n"
        f"- fitness {parent.fitness:.3f}, label {desc.get('label', '?')}\n"
        f"- skill_gain_rate {bd.get('skill_gain_rate', 0):.2f}/h, "
        f"liveness {bd.get('liveness', 0):.2f}, loop {bd.get('loop_penalty', 0):.2f}\n"
        f"- per-seed fitness: {parent.eval.get('per_seed_fitness', [])}\n"
        + anatomy +
        f"- hypothesis that produced it: {parent.hypothesis}\n"
    )


# --- the run ----------------------------------------------------------------

@dataclass
class _CycleOutcome:
    cycle: int
    slot: int
    parent_id: str | None = None
    target_cell: tuple | None = None
    persona: str = ""
    fixed_start: str = ""
    mutation: mutate.MutationResult | None = None
    result: EvalResult | None = None
    error: str = ""
    skipped: bool = False


def seed_archive(arc: Archive, rc: RunConfig) -> Genome | None:
    print(f"[seed] evaluating current HEAD as a root genome "
          f"(persona={rc.persona}, fixed_start={rc.fixed_start or 'off'})…")
    res = run_eval_multi(_eval_cfg(rc, f"{rc.account_prefix}{rc.run_id}seed", 0, None),
                         seeds=rc.seeds)
    if not res.ok:
        print(f"[seed] FAILED: {res.error}")
        return None
    g = _genome_from(arc, res, parent=None, code_ref=mutate.head(REPO),
                     hypothesis="seed: current code", rc=rc)
    r = arc.add(g)
    _pin_ref(g.id, g.code_ref)
    print(observe.observe(res, arc))
    print(f"[seed] {g.id} fitness={g.fitness:.3f} cell={g.cell} -> {r.status}")
    return g


def run(rc: RunConfig) -> Archive:
    arc = Archive(rc.archive_root)
    arc_lock = threading.Lock()
    # Pin the kernel at the HEAD *commit* (revert_kernel does
    # `git checkout <commit> -- foundry/kernel`, which needs a commit ref).
    pinned = safety.head_sha(REPO) or "HEAD"
    print(f"[run] backend={rc.backend} cycles={rc.n_cycles} parallel={rc.parallel} "
          f"window={rc.window_s}s seeds={rc.seeds} fixed_start={rc.fixed_start or 'off'} "
          f"kernel_pin={pinned[:10]} run_id={rc.run_id}")

    # Seed when the grid is empty — or when forced. Forced seeding is how
    # improvements to the BASE genome code enter an existing gene pool:
    # cycle evals check out parent.code_ref, so a new capability at HEAD
    # (e.g. a profession primitive) is invisible to old lineages until a
    # HEAD-based root genome is planted for them to descend from.
    if rc.force_seed or not arc.elites():
        if seed_archive(arc, rc) is None and not arc.elites():
            return arc

    slots: queue.Queue[int] = queue.Queue()
    for k in range(rc.parallel):
        slots.put(k)
    outcomes: queue.Queue[_CycleOutcome] = queue.Queue()

    def job(i: int) -> None:
        slot = slots.get()
        out = _CycleOutcome(cycle=i, slot=slot)
        try:
            if safety.kill_switch_active():
                out.skipped = True
                return
            with arc_lock:
                parent = select.choose_parent(arc, seed=i)
                target = select.suggest_target_cell(arc, seed=i)
                parent_obs = (_parent_observation(arc, parent)
                              + "\n" + observe.history(arc))
            out.parent_id = parent.id if parent else None
            out.target_cell = target
            out.persona, out.fixed_start = _setup_for(rc, parent, target)
            parent_ref = (parent.code_ref if parent and parent.code_ref else "HEAD")
            print(f"[cycle {i}] slot={slot} parent={out.parent_id} target_cell={target} "
                  f"eval_as={out.persona}/{out.fixed_start}")

            wt = _prepare_worktree(slot, parent_ref)

            # --- mutate ----------------------------------------------------
            if rc.backend == "claude":
                mr = mutate.mutate_with_claude(
                    wt, parent_obs, parent, target,
                    timeout=rc.mutate_timeout, model=rc.mutate_model,
                    log_path=ANIMA_ROOT / "data" / "eval_logs" / f"mutate-{rc.run_id}-c{i}.log",
                    eval_setup=f"persona={out.persona}, fixed_start={out.fixed_start}",
                )
            else:
                mr = mutate.mutate_noop(wt, parent)
            out.mutation = mr
            print(f"[cycle {i}] mutation: changed={mr.changed} "
                  f"hypothesis={mr.hypothesis!r}{(' err=' + mr.error) if mr.error else ''}")
            if rc.backend == "claude" and not mr.changed:
                # no variant to score — don't burn an eval window re-running
                # the parent (noop backend exists for that on purpose).
                out.error = f"mutation produced no commit: {mr.error or 'unknown'}"
                return

            # --- anti-gaming: discard any kernel change before anything else
            safety.revert_kernel(wt, pinned)
            if mr.changed and not safety.kernel_is_clean(wt, pinned):
                mutate._commit_all(wt, "foundry: restore pinned kernel")
                out.mutation = mutate.MutationResult(
                    True, code_ref=mutate.head(wt), hypothesis=mr.hypothesis)

            # --- eval the variant from its worktree -------------------------
            user = f"{rc.account_prefix}{rc.run_id}c{i}"
            out.result = run_eval_multi(
                _eval_cfg(rc, user, slot, wt,
                          persona=out.persona, fixed_start=out.fixed_start),
                seeds=rc.seeds,
            )
        except Exception as e:  # noqa: BLE001 — a broken cycle must not kill the run
            out.error = f"{type(e).__name__}: {e}"
        finally:
            outcomes.put(out)
            # NOTE: the slot is released by the MAIN thread after the genome
            # commit is pinned under refs/foundry/ — until then the worktree
            # HEAD is what keeps the variant reachable.

    with ThreadPoolExecutor(max_workers=rc.parallel) as pool:
        submitted = 0
        for i in range(1, rc.n_cycles + 1):
            if safety.kill_switch_active():
                print("[run] STOP file present — not scheduling further cycles.")
                break
            if arc.summary()["total_genomes"] + submitted >= safety.MAX_GENOMES_PER_RUN:
                print("[run] genome cap reached — not scheduling further cycles.")
                break
            pool.submit(job, i)
            submitted += 1

        for _ in range(submitted):
            out = outcomes.get()
            i = out.cycle
            try:
                if out.skipped:
                    print(f"[cycle {i}] skipped (STOP)")
                    continue
                if out.error or out.result is None:
                    print(f"[cycle {i}] cycle FAILED: {out.error or 'no result'}")
                    continue
                if not out.result.ok:
                    print(f"[cycle {i}] eval FAILED: {out.result.error} — skipping insert")
                    continue
                mr = out.mutation
                with arc_lock:
                    g = _genome_from(arc, out.result, parent=out.parent_id,
                                     code_ref=mr.code_ref if mr else "",
                                     hypothesis=mr.hypothesis if mr else "", rc=rc,
                                     target_cell=out.target_cell,
                                     persona=out.persona, fixed_start=out.fixed_start)
                    r = arc.add(g)
                _pin_ref(g.id, g.code_ref)
                prev = f" (prev {r.prev_fitness:.3f})" if r.prev_fitness is not None else ""
                print(f"[cycle {i}] {g.id} fitness={g.fitness:.3f} "
                      f"cell={g.cell} -> {r.status}{prev}")
            finally:
                slots.put(out.slot)

    s = arc.summary()
    print(f"\n[run] done. genomes={s['total_genomes']} filled_cells={s['filled_cells']} "
          f"qd_score={s['qd_score']} best={s['best_fitness']}")
    return arc


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Foundry develop-cycle orchestrator (Phase 1)")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--window", type=int, default=120)
    ap.add_argument("--persona", default="miner")
    ap.add_argument("--backend", choices=["noop", "claude"], default="noop")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--fixed-start", default="miner")
    ap.add_argument("--model", default="sonnet", help="mutator model (cost tiering)")
    ap.add_argument("--mutate-timeout", type=int, default=1500)
    ap.add_argument("--archive", default="foundry/archive")
    ap.add_argument("--seed", action="store_true", dest="force_seed",
                    help="plant a HEAD-based root genome for this persona even "
                         "if the grid is non-empty (use --cycles 0 for pure seeding)")
    args = ap.parse_args(argv)

    rc = RunConfig(
        n_cycles=args.cycles, window_s=args.window, persona=args.persona,
        backend=args.backend, parallel=args.parallel, seeds=args.seeds,
        fixed_start=args.fixed_start, mutate_model=args.model,
        mutate_timeout=args.mutate_timeout, archive_root=args.archive,
        force_seed=args.force_seed,
    )
    run(rc)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
