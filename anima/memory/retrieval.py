"""Memory retrieval — build context blocks for LLM prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anima.memory.database import MemoryDB

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext
    from anima.memory.database import ActionStat


async def retrieve_context(ctx: BrainContext) -> str:
    """Build a memory block to inject into LLM prompts.

    Gathers:
    - Recent episodes at the current location
    - Relationship info for nearby entities
    - Relevant knowledge facts
    - Action success rates
    """
    memory_db: MemoryDB | None = ctx.memory_db
    if memory_db is None:
        return ""

    agent_name = _agent_name(ctx)
    ss = ctx.perception.self_state
    parts: list[str] = []

    # 1. Recent episodes near current location
    episodes = await memory_db.query_episodes(
        agent_name, location_x=ss.x, location_y=ss.y, limit=5
    )
    if episodes:
        ep_lines = []
        for ep in episodes:
            line = f"  - {ep.action}"
            if ep.target:
                line += f" → {ep.target}"
            line += f": {ep.outcome}" if ep.outcome else ""
            if ep.summary:
                line += f" ({ep.summary})"
            ep_lines.append(line)
        parts.append("Recent experiences nearby:\n" + "\n".join(ep_lines))

    # 2. Relationships with nearby entities
    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    if nearby_mobs:
        serials = [m.serial for m in nearby_mobs]
        rels = await memory_db.get_nearby_relationships(agent_name, serials)
        if rels:
            rel_lines = []
            for rel in rels:
                disp_word = _disposition_word(rel.disposition)
                rel_lines.append(
                    f"  - {rel.entity_name}: {disp_word} "
                    f"(trust: {rel.trust:.1f}, met {rel.interaction_count} times)"
                )
            parts.append("People you know nearby:\n" + "\n".join(rel_lines))

    # 3. Knowledge facts
    knowledge = await memory_db.query_knowledge(agent_name, limit=5)
    if knowledge:
        fact_lines = [f"  - {k.fact} (confidence: {k.confidence:.1f})" for k in knowledge]
        parts.append("Things you know:\n" + "\n".join(fact_lines))

    # 4. Action success rates for current context
    context_pattern = _infer_context_pattern(ctx)
    stats = await memory_db.get_action_stats(agent_name, context_pattern)
    if stats:
        stat_lines = _format_action_stats(stats)
        if stat_lines:
            parts.append(
                f"Past experience ({context_pattern}):\n" + "\n".join(stat_lines)
            )

    # 5. Skill Q-values — what the RL system has learned
    from anima.skills.state import encode_state, region_coords

    state_key = encode_state(ctx)
    q_values = await memory_db.get_q_values(agent_name, state_key)
    if q_values:
        q_lines = []
        for action, (q, visits) in sorted(
            q_values.items(), key=lambda x: x[1][0], reverse=True
        ):
            q_lines.append(f"  - {action}: Q={q:.1f} ({visits} tries)")
        parts.append(
            f"Skill learning ({state_key}):\n" + "\n".join(q_lines[:8])
        )

    # 6. What this region is good (and bad) for.
    #
    # ``get_location_values`` tallies *every* non-zero-reward episode here,
    # including net-losses (dying to a mob pack, a failed/blocked action), and
    # returns them ranked by average reward. The block used to flatten all of
    # them under one neutral "This area" header showing ``avg reward +/-X.X``.
    # The sign was buried mid-line, so a region the agent only ever lost in
    # read to the LLM identically to a region that reliably paid off — the
    # whole point of this channel (the sibling accessor is literally named
    # ``get_best_locations``) is to tell the agent where activities *work*.
    # Worse, when every activity in a region is net-negative the least-bad
    # loss surfaced first, framed exactly like a recommendation, nudging the
    # agent back toward a place that has only ever harmed it.
    #
    # Split by sign: positive-average activities are surfaced as "pays off"
    # (a recommendation), negative-average ones as "costly — avoid" (a
    # warning). Neither list is dropped, so the warning signal in the losses
    # is preserved instead of masquerading as advice.
    #
    # The cap must be applied *per section*, never to the raw list. get_location_values
    # returns rows ordered by average reward DESC, so the costly (negative-average)
    # activities always sort last. Slicing loc_values[:5] before the split therefore
    # silently discarded the warnings whenever a region had more than five activities
    # (e.g. several profitable ones plus a couple of lethal ones): the agent would be
    # told only where things pay off and never warned away from where it kept dying —
    # the exact failure this block's redesign claimed to prevent. Split first over the
    # full list, then cap each section independently.
    #
    # The cap direction also matters per section. get_location_values orders rows by
    # average reward DESC, so the negative-average ("avoid") activities arrive
    # least-costly first and the deadliest last. Capping the avoid section by simply
    # keeping the first five negatives encountered therefore surfaces the *mildest*
    # losses and silently discards the genuinely lethal ones — the opposite of what a
    # warning is for. The pays-off section wants the top five highest averages (the
    # natural DESC head); the avoid section wants the five *worst* averages, presented
    # worst-first. Collect every negative row, then keep its lowest-average tail.
    rx, ry = region_coords(ss.x, ss.y)
    loc_values = await memory_db.get_location_values(agent_name, rx, ry)
    if loc_values:
        good_lines: list[str] = []
        bad_rows: list[tuple[float, str]] = []
        for activity, total_r, visits in loc_values:
            avg = total_r / visits if visits else 0
            line = f"  - {activity}: avg reward {avg:+.1f} ({visits} visits)"
            if avg > 0:
                if len(good_lines) < 5:
                    good_lines.append(line)
            elif avg < 0:
                bad_rows.append((avg, line))
        # Keep the five most costly (lowest average) activities, worst first, so the
        # warning steers the agent away from where it has lost the most — not where
        # it lost the least.
        bad_rows.sort(key=lambda r: r[0])
        bad_lines = [line for _, line in bad_rows[:5]]
        if good_lines:
            parts.append(
                f"This area (region {rx},{ry}) pays off for:\n"
                + "\n".join(good_lines)
            )
        if bad_lines:
            parts.append(
                f"This area (region {rx},{ry}) has been costly — avoid:\n"
                + "\n".join(bad_lines)
            )

    if not parts:
        return ""

    return "== Your Memory ==\n" + "\n\n".join(parts)


def _format_action_stats(stats: list[ActionStat]) -> list[str]:
    """Render action stats as LLM lines, ranked by the metric we display.

    ``get_action_stats`` returns rows ordered by raw ``total_reward`` (DESC),
    but the line we show the LLM reports *average* reward per attempt. A
    high-volume mediocre action (many tries, small per-try reward) accumulates
    a large cumulative total and would otherwise be listed above a rarely-tried
    but genuinely better action — contradicting the ``avg reward`` figure right
    next to it. Re-rank by average so the ordering matches what's printed, the
    same per-attempt semantics already used for location values.
    """
    rendered: list[tuple[float, str]] = []
    for s in stats:
        total = s.successes + s.failures
        if total == 0:
            continue
        avg_reward = s.total_reward / total
        rendered.append(
            (
                avg_reward,
                f'  - "{s.action}": {s.successes}/{total} success '
                f"(avg reward: {avg_reward:+.1f})",
            )
        )
    rendered.sort(key=lambda r: r[0], reverse=True)
    return [line for _, line in rendered]


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"


def _disposition_word(d: float) -> str:
    if d >= 0.5:
        return "friendly"
    if d >= 0.1:
        return "acquaintance"
    if d >= -0.1:
        return "neutral"
    if d >= -0.5:
        return "wary"
    return "hostile"


def _infer_context_pattern(ctx: BrainContext) -> str:
    """Infer a rough context pattern from the current game state."""
    # Must match brain.think._infer_context_pattern exactly: the write path
    # (update_action_stats) and this read path bucket by the same key, so a
    # divergence here would look up an empty bucket. Gate "near_player" on the
    # human body, not notoriety (which flags every hostile mob as a player).
    from anima.skills.state import has_player_nearby

    ss = ctx.perception.self_state
    has_players = has_player_nearby(ctx)

    if ss.hp_percent < 30:
        return "low_hp"
    if has_players:
        return "near_player"
    return "exploring"
