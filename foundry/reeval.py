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
from foundry.orchestrator import LANE_BUDGET, _prepare_worktree


# Below this magnitude a recorded fitness has no usable SCALE to normalise the
# held-out score against. fitness is ``gate * inner`` (eval.fitness), so a barely
# viable genome (a near-zero viability gate) archives a tiny non-zero total like
# 1e-9 — NOT exactly 0.0. Dividing by abs(1e-9) turns a normal held-out recovery
# into an astronomically large finite ratio, which then survives the finite
# filter in _summarize_ratios and single-handedly swamps the cross-genome median.
_NEGLIGIBLE_FITNESS = 1e-6

# The ~0-recorded guard below only fires UNDER ``_NEGLIGIBLE_FITNESS`` (1e-6),
# but ``fitness = gate * inner`` produces a whole CONTINUUM of small-but-not-tiny
# recorded scores: a low-viability-gate genome can archive e.g. 5e-4 (gate ~1e-3
# × inner ~0.5) — well ABOVE 1e-6, yet still no usable scale. A held-out re-run
# that recovered a real score (say 120) then yields a *finite* shortfall ratio in
# the hundreds of thousands that slips past the inf-filter in _summarize_ratios
# and swamps the cross-genome median exactly as the sub-1e-6 case did. That is
# not a "fraction recovered" — it is an over-replication. Any ratio above this
# ceiling (recovered far more than recorded) is reclassified as one, so the
# verdict bands (≤0.8 replicates) and the legitimate small over-replications
# (1.2, 1.5) are untouched while the absurd outliers join the inf bucket.
_OVER_REPLICATION_CEILING = 10.0


def _elite_reeval_order(elites: list[Genome]) -> list[Genome]:
    """Order grid elites so the LEAST trustworthy champion is re-checked FIRST.

    ``--elites`` is the demotion-evidence pass: it re-runs each champion on fresh
    accounts to see whether its archived fitness replicates. The queue used to be
    sorted by raw ``g.fitness`` (descending), but raw fitness is the very signal
    the rest of the pipeline distrusts — selection (``select._selection_quality``),
    the REFLECT "proven recipes" list (``observe.history``) and the kernel
    promotion rule all rank champions by the variance-aware lower bound
    ``min(fitness, reliability)`` (reliability = mean − λ·pstdev), which both
    discounts a lucky high-variance elite (per_seed [200, 20] → quality 20, not
    its 110 mean) and honors the human held-out corrections baked into
    ``eval.fitness``.

    Sorting the re-eval queue by raw fitness floats exactly those lucky/inflated
    champions — the ones MOST in need of a replication check — to the BOTTOM of a
    long ``--elites`` run. A live held-out pass costs ~window_s per genome and is
    routinely time-boxed or interrupted (STOP/operator), so the suspect champions
    that should be evidenced first were the ones silently re-checked last (or
    never). Rank by ``_selection_quality`` ASCENDING so the steadiest, most-trusted
    elites yield their slot to the volatile ones, with the genome id as a stable
    tiebreak. This only reorders the queue; every elite is still re-eval'd and the
    grid is never touched (the tool stays report-only).
    """
    # Function-scope import mirrors observe.history / _archive_context_lines and
    # avoids a reeval<->select import edge (select is a leaf of the kernel).
    from foundry.select import _selection_quality

    return sorted(elites, key=lambda g: (_selection_quality(g), g.id))


def _replication_ratio(recorded: float, held_out: float) -> float:
    """How much of ``recorded`` fitness the held-out re-run reproduced.

    The verdict bands (>=0.8 replicates, 0.5-0.8 weak, <0.5 demote) read this as
    "fraction of the archived score that came back". A bare ``held_out/recorded``
    INVERTS the moment recorded fitness is non-positive — and it can be: fitness
    is ``gate * inner`` where ``inner`` includes a net-worth term off
    ``gold_delta``, so a genome that bled gold (COMBAT/economic loops) archives a
    negative total. Then a held-out run that got WORSE (more negative) yields a
    ratio > 1 ("replicates"), while one that IMPROVED yields a ratio < 1 ("does
    not replicate") — exactly backwards for the one tool whose job is to surface
    demotion evidence.

    Use a shortfall form that is monotone in held_out and matches the old
    positive-fitness behaviour exactly: ``1 - (recorded - held_out)/|recorded|``.
    Higher held_out always scores higher; for recorded > 0 it reduces to
    ``held_out / recorded``. A recorded fitness AT OR BELOW ``_NEGLIGIBLE_FITNESS``
    in magnitude (``~0`` — exactly 0.0 OR a tiny ``gate*inner`` residue like 1e-9)
    has no scale to normalise against: dividing by ``abs(recorded)`` there yields
    an arbitrarily large FINITE ratio that slips past the inf-filter in
    ``_summarize_ratios`` and dominates the median demotion verdict. Treat the
    whole near-zero band like exact zero — a held_out that also recovered ~0
    replicates (ratio 1) while any positive recovery reads as a clean
    over-replication (``inf``, which the summary already excludes from the median).
    """
    if abs(recorded) <= _NEGLIGIBLE_FITNESS:
        return 1.0 if held_out <= 0.0 else float("inf")
    ratio = 1.0 - (recorded - held_out) / abs(recorded)
    # A small-but-above-threshold recorded fitness with a much larger held-out
    # recovery still has no meaningful finite scale: the shortfall ratio explodes
    # into the hundreds/thousands. Treat that as the SAME over-replication the
    # ~0-recorded branch returns ``inf`` for, so _summarize_ratios sets it aside
    # (counted separately) instead of folding a multi-thousand finite value into
    # the headline demotion median. The ceiling sits far above every legitimate
    # verdict band (replicates tops out at ~1.0; a real over-replication like 1.5
    # stays finite) so only the genuinely scale-free outliers are reclassified.
    if ratio > _OVER_REPLICATION_CEILING:
        return float("inf")
    return ratio


def _lane_safe_seeds(seeds: int, slot: int) -> int:
    """Largest seed count whose per-slot expanded lane block fits the GM table.

    run_eval_multi fans seed k onto ``cfg.lane + k`` (k in 0..seeds-1) and the GM
    indexes the workplace as ``LANE_SPOTS[lane % len(LANE_SPOTS)]`` (gm.py). With
    ``lane = slot * seeds`` the highest expanded lane is ``(slot+1)*seeds - 1``;
    once that reaches ``LANE_BUDGET`` (== len(LANE_SPOTS)) the lane wraps modulo
    and two of THIS genome's concurrent seed evals share one workplace — the
    multi-hour fixed-start collision the orchestrator's lane budget exists to
    prevent. The orchestrator clamps ``parallel*seeds``; reeval had no equivalent
    guard, so ``reeval --seeds N`` with N (or (slot+1)*N) > LANE_BUDGET silently
    wedged. Bound the block to the table: ``(slot+1)*seeds <= LANE_BUDGET``.
    """
    return max(1, min(seeds, LANE_BUDGET // (slot + 1)))


def reeval_genome(arc: Archive, g: Genome, seeds: int, window_s: int,
                  slot: int = 0) -> dict:
    cfg_src = g.config or {}
    persona = cfg_src.get("persona", "miner")
    fixed_start = cfg_src.get("fixed_start", "miner")
    wt = _prepare_worktree(slot, g.code_ref or "HEAD")

    # Clamp seeds so this slot's expanded lane block stays inside the GM table
    # (no modulo wrap → no co-located fixed-start setups). See _lane_safe_seeds.
    safe_seeds = _lane_safe_seeds(seeds, slot)
    if safe_seeds != seeds:
        print(f"[reeval] seeds {seeds} would overrun the GM lane budget "
              f"({LANE_BUDGET} lanes, slot {slot}); clamping seeds -> {safe_seeds}")
    seeds = safe_seeds

    # Each slot owns a contiguous block of `seeds` lanes AND ports: run_eval_multi
    # fans out the seeds as lane+k / proxy_port+k / web_port+k for k in 0..seeds-1.
    # The ports already stride by `slot * seeds`, but lane was pinned to 0 — so two
    # concurrent reeval slots put their agents on the SAME GM workplace lanes
    # (0..seeds-1) and their fixed-start setups collide (the lane-collision class
    # the orchestrator's lane budget exists to prevent). Stride lane with slot too.
    span = max(1, seeds)
    cfg = EvalConfig(
        account_user=f"re{g.id.replace('g_', '')}",
        persona=persona,
        fixed_start=fixed_start,
        window_s=window_s,
        proxy_port=2680 + slot * span,
        web_port=8200 + slot * span,
        lane=slot * span,
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
        out["ratio"] = _replication_ratio(g.fitness, res.score)
    else:
        out["error"] = res.error
    return out


def _summarize_ratios(ratios: list[float]) -> str | None:
    """Human-readable summary of the per-genome replication ratios, or None for
    fewer than two data points.

    ``_replication_ratio`` deliberately returns ``float("inf")`` when a genome's
    recorded fitness was ~0 but the held-out re-run recovered a positive score
    (a clean over-replication — there is no finite scale to normalise against).
    That per-genome sentinel is fine, but feeding it straight into
    ``statistics.median`` POISONS the cross-genome verdict: a single ``inf`` at
    (or straddling) the median position makes the whole summary read ``inf`` even
    when most genomes clearly FAILED to replicate (e.g. median of
    ``[0.2, 0.4, inf, inf]`` is ``inf``). The median is this tool's headline
    demotion-evidence number, so a lone over-replicating genome silently erasing
    a fleet of demotion signals defeats its purpose.

    Take the median over the FINITE ratios (the genomes that have a meaningful
    fraction-recovered), and report the over-replications as a separate count so
    no information is lost. With nothing but over-replications, say so plainly.
    """
    if len(ratios) < 2:
        return None
    finite = [r for r in ratios if r != float("inf")]
    n_over = len(ratios) - len(finite)
    over = f"  ({n_over} over-replicated: recorded ~0, recovered positive)" if n_over else ""
    if not finite:
        return (f"median replication ratio: n/a — all {len(ratios)} genomes "
                f"over-replicated (recorded ~0, recovered positive)")
    return (f"median replication ratio: {statistics.median(finite):.2f} "
            f"over {len(finite)} genomes{over}")


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


def _is_cell_comparable(r: dict) -> bool:
    """Whether ``r``'s held-out run is valid replication evidence for the cell
    its archived fitness scores.

    The replication ratio normalises the held-out score against the genome's
    RECORDED fitness, which was earned in ``r["cell"]``. When the re-run DRIFTS
    to a different behavioral cell (``held_out_cell != cell``) the two numbers
    describe different niches, so the ratio is not a like-for-like replication
    measure — exactly why ``_pool_confirmation`` (orchestrator) refuses to pool
    an off-cell confirm round into the cell's reliability bound. A failed eval is
    not comparable either.
    """
    return bool(r.get("ok")) and list(r.get("held_out_cell", [])) == list(r["cell"])


def _comparable_ratios(results: list[dict]) -> tuple[list[float], int]:
    """Split ok re-runs into (ratios that legitimately measure replication for
    their archived cell, count of ok runs DROPPED for cell drift).

    The cross-genome median is this tool's headline demotion-evidence number and
    must summarise like-for-like replications only; an off-cell re-run's ratio
    (e.g. a COMBAT|2 archive whose re-run landed COMBAT|1) compares two niches
    and would skew that median the same way a doubled or ``inf`` entry would.
    Drifted genomes are still reported per-line via ``_verdict`` ("CELL DRIFT");
    they are only withheld from the aggregate.
    """
    ratios = [r["ratio"] for r in results if _is_cell_comparable(r)]
    n_drift = sum(1 for r in results if r.get("ok") and not _is_cell_comparable(r))
    return ratios, n_drift


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
        targets = _elite_reeval_order(arc.elites())
    for gid in args.genomes:
        g = arc.get(gid)
        if g is None:
            print(f"{gid}: not found")
            continue
        targets.append(g)
    # De-duplicate by genome id (first occurrence wins, preserving order). A
    # genome named explicitly that is ALSO a grid elite would otherwise be
    # re-eval'd twice — burning a second ~window_s live eval for no new evidence
    # AND counting its ratio twice in the median replication verdict, which is
    # this tool's whole output. The median is meant to summarise N DISTINCT
    # genomes; a doubled entry silently pulls it toward that one genome.
    seen: set[str] = set()
    deduped: list[Genome] = []
    for g in targets:
        if g.id in seen:
            continue
        seen.add(g.id)
        deduped.append(g)
    targets = deduped
    if not targets:
        print("nothing to re-eval (pass genome ids or --elites)")
        return 2

    results: list[dict] = []
    for g in targets:
        # A legacy / hand-edited genome record can carry ``"config": null`` (the
        # JSON loads to ``None`` via ``Genome.from_dict``'s ``d.get("config",{})``,
        # which returns the explicit null when the KEY exists). ``g.config.get(…)``
        # would then raise ``AttributeError`` and abort the WHOLE ``--elites``
        # demotion-evidence pass mid-run — silently dropping every genome queued
        # after the bad one (the same one-bad-record-aborts-the-batch class the
        # score.py loop and _summarize_ratios were hardened against). reeval_genome
        # already guards with ``g.config or {}``; this status line must too.
        cfg = g.config or {}
        print(f"[reeval] {g.id} cell={g.cell} recorded={g.fitness:.3f} "
              f"persona={cfg.get('persona')}/{cfg.get('fixed_start')} …")
        r = reeval_genome(arc, g, seeds=args.seeds, window_s=args.window)
        if r["ok"]:
            print(f"[reeval] {g.id}: held-out {r['held_out']:.3f} "
                  f"(seeds {[round(v, 2) for v in r['held_out_seeds']]}) "
                  f"ratio {r['ratio']:.2f} → {_verdict(r)}")
            results.append(r)
        else:
            print(f"[reeval] {g.id}: {_verdict(r)}")

    # The headline median summarises like-for-like replications only: an off-cell
    # re-run's ratio compares two different niches, so it is reported per-line
    # (CELL DRIFT) but kept OUT of the aggregate.
    ratios, n_drift = _comparable_ratios(results)
    summary = _summarize_ratios(ratios)
    if summary:
        print(f"\n[reeval] {summary}")
    if n_drift:
        print(f"[reeval] {n_drift} genome(s) excluded from the median: held-out "
              f"cell drifted from the archived cell (not like-for-like)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
