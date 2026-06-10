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
    lines.append("")

    smells = _smells(res)
    if smells:
        lines.append("## Attention (heuristic hints, not fitness)")
        for sm in smells:
            lines.append(f"- {sm}")
        lines.append("")

    if archive is not None:
        from foundry.select import empty_cells
        empties = empty_cells(archive)
        lines.append("## Archive context")
        lines.append(f"- filled cells {archive.filled_cells()}, QD-score {archive.qd_score():.3f}")
        if empties:
            shown = ", ".join(cell_to_str(c) for c in empties[:8])
            lines.append(f"- empty cells to explore: {shown}{' …' if len(empties) > 8 else ''}")
        lines.append("")

    return "\n".join(lines)
