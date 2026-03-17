"""Memory retrieval — build context blocks for LLM prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anima.memory.database import MemoryDB

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext


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
        stat_lines = []
        for s in stats:
            total = s.successes + s.failures
            if total == 0:
                continue
            avg_reward = s.total_reward / total
            stat_lines.append(
                f"  - \"{s.action}\": {s.successes}/{total} success "
                f"(avg reward: {avg_reward:+.1f})"
            )
        if stat_lines:
            parts.append(
                f"Past experience ({context_pattern}):\n" + "\n".join(stat_lines)
            )

    if not parts:
        return ""

    return "== Your Memory ==\n" + "\n\n".join(parts)


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
    ss = ctx.perception.self_state
    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)

    has_players = any(
        m.notoriety is not None and m.notoriety.value <= 6
        for m in nearby_mobs
    )

    if ss.hp_percent < 30:
        return "low_hp"
    if has_players:
        return "near_player"
    return "exploring"
