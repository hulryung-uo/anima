"""Observation — assemble eval evidence for the mutator (FOUNDRY.md §6).

Repurposes the old tools/diagnose.py idea, but inverts the intelligence: this
module only *assembles the evidence*; the hypothesis is formed by the LLM mutator
(foundry/mutate.py), not by hardcoded rules. The heuristic "smells" below are
hints to focus the LLM's attention — they never affect fitness (that is the
kernel's job). Mutator-editable.
"""

from __future__ import annotations

from foundry.kernel.archive import Archive, cell_to_str
from foundry.kernel.eval import EvalResult


def _smells(res: EvalResult) -> list[str]:
    """Heuristic attention hints (not fitness — just where to look)."""
    s = res.summary
    f = res.fitness
    out: list[str] = []
    if f.liveness < 0.3:
        out.append(
            f"LOW LIVENESS ({f.liveness:.2f}): agent took only {s.total_actions} "
            f"actions in {s.duration_s:.0f}s — mostly idle/blocked, not playing."
        )
    if f.loop_penalty > 0.3:
        out.append(
            f"STUCK WALKING ({f.loop_penalty:.2f}): {s.steps_denied} denied vs "
            f"{s.steps_confirmed} confirmed steps — pathing into obstacles."
        )
    if f.skill_gain_rate < 0.5 and s.duration_h > 0.05:
        out.append(
            "NOT PRACTICING ITS TRADE: ~0 skill gain — the agent isn't doing the "
            "work that defines its profession (fitness backbone is skill gain)."
        )
    if s.death_count > 0:
        out.append(f"DIED {s.death_count}x: survival is gating fitness "
                   f"(alive={f.alive_fraction:.2f}).")
    if not s.items_into_pack and f.skill_gain_rate < 0.5:
        out.append("NO OUTPUT: nothing gathered/crafted and no skill gained "
                   "— net unproductive.")
    return out


def _is_dead_end(g) -> bool:
    """A mutation the mutator should NOT re-walk: it produced no trade progress
    (zero skill gain), was barely alive/active (low viability gate), or aimed at
    one cell and landed in another. These are exactly the lessons history()
    exists to surface (e.g. three meditation mutations that all earned zero
    skill gain).
    """
    bd = g.eval.get("breakdown", {})
    if bd.get("skill_gain_rate", 1) == 0:
        return True
    if bd.get("viability_gate", 1) < 0.5:
        return True
    target = g.config.get("target_cell")
    if target and list(target) != list(g.cell):
        return True
    return False


def _reflect_window(gs: list, limit: int) -> list:
    """The most instructive ``limit`` genomes to feed the mutator's REFLECT log.

    Pure global recency (``gs[-limit:]``) silently evicts the dead-end attempts
    the log is FOR: across a long run, newer unrelated cycles push the
    zero-gain / aimed-but-missed failures out of the window, so the mutator
    re-attempts the same dead end it has no memory of. Keep every dead end that
    still fits, then backfill with the most recent genomes — preserving
    chronological (disk/id) order so the narrative still reads oldest->newest.

    But dead-end retention must NOT crowd out the just-evaluated cycle. The
    newest genome (``gs[-1]``) is the eval the orchestrator literally just ran
    and is asking the mutator to react to; once a long run accumulates ``limit``
    or more OLD dead ends, ``dead_ends[-limit:]`` fills the budget entirely and
    the backfill loop never runs, so the latest cycle's outcome vanishes from
    the REFLECT log and the mutator gets zero feedback on its own last move.
    Reserve the newest genome's slot up front so it always survives the cull.
    """
    if len(gs) <= limit:
        return gs
    dead_ends = [g for g in gs if _is_dead_end(g)]
    # Always keep the just-evaluated cycle (gs[-1]); then fill the remaining
    # budget with the newest dead ends; then backfill with the most recent
    # remaining genomes. Finally restore chronological order.
    keep = {id(gs[-1])}
    for g in reversed(dead_ends):
        if len(keep) >= limit:
            break
        keep.add(id(g))
    for g in reversed(gs):
        if len(keep) >= limit:
            break
        keep.add(id(g))
    return [g for g in gs if id(g) in keep]


def history(archive: Archive, limit: int = 12) -> str:
    """REFLECT-lite (FOUNDRY.md §6 step 7): what was tried and what happened.

    The mutator reads this so it stops re-walking dead ends (e.g. three
    independent meditation mutations that all earned zero skill gain). Drawn
    from the archive's full lineage, not just grid elites.
    """
    gs = archive.all_genomes()
    if not gs:
        return ""
    # Rank the "PROVEN recipes" list the mutator mines by the SAME variance-aware
    # signal select.py picks parents and the kernel promotes on — NOT raw
    # g.fitness. _selection_quality = min(fitness, reliability) (reliability =
    # mean − λ·pstdev): it both discounts lucky high-variance elites (a single
    # 400/0 pair averages to 200 but its lower bound is 0) and honors the human
    # held-out corrections baked into eval.fitness for the ruler-inflation
    # genomes. Sorting these by raw fitness floats exactly those demoted/volatile
    # genomes to the TOP of the list captioned "the PROVEN recipes (mine these
    # for tricks)", steering the LLM mutator to copy the recipe the rest of the
    # pipeline deliberately distrusts. (Function-scope import mirrors
    # _archive_context_lines and avoids a select<->observe module cycle.)
    from foundry.select import _selection_quality, targetable_cells

    # Mirror the targetable-cell filter the rest of the pipeline already applies
    # (select.TARGET_PROFESSIONS, _archive_context_lines, status.render): the
    # kernel's archive.elites() still INCLUDES a NONE-fallback-row elite — a
    # failed agent that gained no trade skill and banked a high-variance NONE
    # score (the scoring artifact the held-out corrections flagged, e.g.
    # g_00118). Listing it HERE under "the PROVEN recipes (mine these for
    # tricks)" tells the LLM mutator to copy a recipe that produced NO
    # profession progress — the exact recipe select never targets, the archive
    # panel never counts, and status never tables. Drop the NONE row so the
    # recipe list describes the same trusted universe as every other consumer.
    targetable = {cell_to_str(c) for c in targetable_cells()}
    proven = [g for g in archive.elites() if cell_to_str(g.cell) in targetable]
    lines = ["## Current cell elites — the PROVEN recipes (mine these for tricks)"]
    # Display the SAME variance-aware quality the list is ORDERED by, not the raw
    # fitness mean. The list was migrated to sort by _selection_quality (=
    # min(fitness, reliability); reliability = mean − λ·pstdev) precisely so a
    # lucky high-variance or human-demoted genome can't head the recipe list — but
    # the printed number stayed ``e.fitness`` (the raw mean). That number is what
    # the LLM mutator actually reads, so a coin-flip elite (per_seed [400, 0],
    # mean 200, quality 0) still ADVERTISED itself as a "200.00 proven recipe" even
    # though it correctly sorted last, and a ruler-inflation genome demoted to
    # fitness 3.06 but whose preserved-inflated per_seed re-inflates reliability
    # would mis-advertise the inflated mean. Lead with the trust signal (q…) that
    # drives the ordering so the displayed number and the ranking agree, and keep
    # the raw mean visible (labelled) so no information is lost — the same
    # display-matches-trust consistency status.py / select.py already enforce.
    for e in sorted(proven, key=lambda g: -_selection_quality(g)):
        q = _selection_quality(e)
        lines.append(
            f"- {e.cell} q{q:.2f} (mean {e.fitness:.2f}) ({e.id}): “{e.hypothesis}”")
    lines.append("")
    lines.append("## Prior mutations and what actually happened (learn from these)")
    for g in _reflect_window(gs, limit):
        bd = g.eval.get("breakdown", {})
        notes: list[str] = []
        if bd.get("skill_gain_rate", 1) == 0:
            notes.append("ZERO skill gain")
        if bd.get("viability_gate", 1) < 0.5:
            notes.append(f"gate {bd['viability_gate']:.2f} (barely alive/active)")
        target = g.config.get("target_cell")
        if target and list(target) != list(g.cell):
            notes.append(f"aimed {tuple(target)} but landed {g.cell}")
        note = ("  ⚠ " + "; ".join(notes)) if notes else ""
        lines.append(
            f"- {g.id} (parent {g.parent or '—'}) fitness {g.fitness:.2f} "
            f"cell {g.cell}: “{g.hypothesis}”{note}"
        )
    lines.append("")
    return "\n".join(lines)


def _archive_context_lines(archive: Archive) -> list[str]:
    """The mutator's "Archive context" panel — filled/QD-score + empty cells.

    The headline figures here MUST describe the SAME universe as the
    "empty cells to explore" list printed right below them, which comes from
    ``select.empty_cells`` (TARGETABLE cells only — the NONE fallback row is
    deliberately excluded; see select.TARGET_PROFESSIONS). The kernel's
    ``Archive.filled_cells()`` is ``len(grid)`` and ``Archive.qd_score()`` sums
    fitness over EVERY grid elite — both of which include a NONE-row elite (the
    high-variance scoring artifact the backlog flags, e.g. g_00118) or any cell
    whose profession/sociability is outside the active enumeration. Reporting
    those next to a targetable-only empty list makes the panel contradict
    itself: "filled cells N" counts ground the empty list says is un-fillable,
    and the QD-score is a sum no targetable cell accounts for — the exact
    headline-vs-body inconsistency status.py was hardened against. Derive the
    filled count (against the targetable denominator) and the QD-score from the
    targetable-cell elite set so the whole panel describes one universe.
    """
    from foundry.select import empty_cells, targetable_cells

    targetable = {cell_to_str(c) for c in targetable_cells()}
    elites = [g for g in archive.elites() if cell_to_str(g.cell) in targetable]
    filled = len(elites)
    qd_score = sum(g.fitness for g in elites)
    empties = empty_cells(archive)
    lines = ["## Archive context",
             f"- filled cells {filled}/{len(targetable)}, QD-score {qd_score:.3f}"]
    if empties:
        shown = ", ".join(cell_to_str(c) for c in empties[:8])
        lines.append(f"- empty cells to explore: {shown}{' …' if len(empties) > 8 else ''}")
    lines.append("")
    return lines


def observe(res: EvalResult, archive: Archive | None = None) -> str:
    """Render a markdown observation of one eval for the mutator to read."""
    if not res.ok:
        return f"# Eval FAILED\n\n{res.error}\n"

    s = res.summary
    f = res.fitness
    d = res.descriptor
    lines: list[str] = []

    lines.append("# Eval Observation")
    lines.append("")
    lines.append(f"- **fitness {f.total:.3f}**  (cell {d.cell} — {d.label()})")
    lines.append(f"- duration {s.duration_s:.0f}s, {s.line_count} packets, "
                 f"{s.parse_errors} parse errors")
    lines.append("")
    lines.append("## Where fitness came from / was lost")
    lines.append(f"- viability_gate **{f.viability_gate:.2f}** "
                 f"(alive {f.alive_fraction:.2f} × liveness {f.liveness:.2f} × "
                 f"(1−loop {f.loop_penalty:.2f}))")
    lines.append(f"- skill_term {f.skill_term:.3f}  "
                 f"(skill gain {f.skill_gain_rate:.2f}/h) ← BACKBONE")
    lines.append(f"- worth_term {f.worth_term:.3f}  (gold {f.networth_rate:.0f}/h)")
    lines.append(f"- produce_term {f.produce_term:.3f}  "
                 f"({len(s.items_into_pack)} items gathered/crafted)")
    lines.append(f"- behavior_bonus {f.behavior_bonus:.3f}  (regions {f.regions_rate:.1f}/h)")
    lines.append("")
    lines.append("## What the agent actually did")
    lines.append(f"- actions: {s.action_counts} (total {s.total_actions})")
    lines.append(f"- movement: {s.steps_confirmed} steps, {s.unique_regions} regions, "
                 f"{s.steps_denied} denied")
    if s.skills:
        gained = sorted((sk for sk in s.skills.values() if sk.gain > 0),
                        key=lambda sk: -sk.gain)
        if gained:
            lines.append("- skills gained: " + ", ".join(
                f"{sk.name} +{sk.gain:.1f}" for sk in gained[:6]))
        else:
            lines.append("- skills gained: none")
    lines.append(f"- gold delta {s.gold_delta}, items to pack {len(s.items_into_pack)}")
    lines.append(f"- speech sent/recv {s.speech_sent}/{s.speech_recv}, "
                 f"attacks {s.attacks_initiated}")
    env = s.entities_seen
    note = " — the workplace is EMPTY: combat/begging/social hypotheses have no targets" \
        if env == 0 else ""
    lines.append(f"- entities seen nearby during eval: {env}{note}")
    lines.append("")

    smells = _smells(res)
    if smells:
        lines.append("## Attention (heuristic hints, not fitness)")
        for sm in smells:
            lines.append(f"- {sm}")
        lines.append("")

    if archive is not None:
        lines.extend(_archive_context_lines(archive))

    return "\n".join(lines)
