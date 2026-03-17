"""Learning module — reflection loop and pattern extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.memory.database import MemoryDB

if TYPE_CHECKING:
    from anima.brain.llm import LLMClient

logger = structlog.get_logger()

# Number of episodes between reflection cycles
REFLECT_INTERVAL = 20


async def reflect(
    memory_db: MemoryDB,
    llm: LLMClient,
    agent_name: str,
) -> list[str]:
    """Summarize recent episodes into knowledge facts using LLM.

    Returns the list of new facts extracted.
    """
    episodes = await memory_db.query_episodes(agent_name, limit=REFLECT_INTERVAL)
    if len(episodes) < 5:
        return []

    # Build episode summaries for the LLM
    ep_lines = []
    for ep in episodes:
        line = f"- {ep.action}"
        if ep.target:
            line += f" → {ep.target}"
        line += f" at ({ep.location_x},{ep.location_y})"
        line += f": {ep.outcome}" if ep.outcome else ""
        if ep.reward:
            line += f" (reward: {ep.reward:+.1f})"
        if ep.summary:
            line += f" — {ep.summary}"
        ep_lines.append(line)

    prompt = f"""\
You are analyzing the recent experiences of {agent_name}, an adventurer in Britannia.

Recent episodes:
{chr(10).join(ep_lines)}

Extract 1-3 useful facts or patterns from these experiences.
Each fact should be a single concise sentence that would help future decision-making.
Examples: "The bank is at (1434, 1699)", "hulryung speaks Korean and is friendly",
"Walking near (1550, 1620) often gets blocked".

Reply with ONLY the facts, one per line. No numbering, no bullets, no extra text."""

    result = await llm.chat([
        {"role": "system", "content": "You extract knowledge from experience logs. Be concise."},
        {"role": "user", "content": prompt},
    ])

    if not result.text:
        return []

    # Parse facts from LLM response
    new_facts: list[str] = []
    for line in result.text.strip().splitlines():
        fact = line.strip().lstrip("-•*0123456789.) ")
        if fact and len(fact) > 10:
            await memory_db.add_knowledge(
                agent_name, fact, source="reflection", confidence=0.5
            )
            new_facts.append(fact)
            logger.info("knowledge_extracted", fact=fact)

    return new_facts
