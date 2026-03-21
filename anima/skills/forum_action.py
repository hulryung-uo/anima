"""Forum BT action node — read/write forum posts from the behavior tree."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from anima.skills.forum import ForumClient

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()


async def forum_read_action(ctx: BrainContext) -> "Status":
    """Search the forum library when the agent needs knowledge.

    Triggered by:
    - skill_problem in blackboard (tool broke, need materials, etc.)
    - Periodic library browse (read_interval cooldown)
    """
    from anima.brain.behavior_tree import Status

    forum: ForumClient | None = ctx.blackboard.get("forum_client")
    if forum is None or ctx.memory_db is None:
        return Status.FAILURE

    memory_db = ctx.memory_db
    agent_name = _agent_name(ctx)
    now = time.time()
    last_read = ctx.blackboard.get("forum_last_read", 0.0)
    read_interval = ctx.cfg.forum.read_interval

    # Check if there's a pending problem that needs research
    problem = ctx.blackboard.get("skill_problem")
    need_research = problem is not None

    if not need_research and now - last_read < read_interval:
        return Status.FAILURE

    ctx.blackboard["forum_last_read"] = now

    if need_research and problem:
        # Search library for relevant knowledge
        query = _extract_search_query(problem)
        logger.info("forum_research", query=query, problem=problem[:60])
        posts = await forum.search(query)
        if not posts:
            # Broader fallback — search library by category
            posts = await forum.read_posts("library", limit=10)
    else:
        # Periodic browse — read library for general knowledge
        posts = await forum.read_posts("library", limit=5)

    new_facts = 0
    for post in posts[:5]:  # limit to 5 to avoid flooding
        if post.author == agent_name:
            continue
        # Store title + first ~200 chars of body as knowledge
        snippet = post.body[:200].replace("\n", " ").strip()
        fact = f"[Library] {post.title}: {snippet}"
        await memory_db.add_knowledge(
            agent_name, fact,
            source=f"library:{post.post_id[:8]}",
            confidence=0.7,
        )
        new_facts += 1

    if new_facts:
        logger.info("forum_learned", facts=new_facts, research=need_research)

    return Status.SUCCESS


def _extract_search_query(problem: str) -> str:
    """Extract a search query from a problem description."""
    problem_lower = problem.lower()
    # Map common problems to library search terms
    if "saw" in problem_lower or "carpentry" in problem_lower:
        return "carpentry"
    if "tinker" in problem_lower:
        return "tinkering"
    if "hatchet" in problem_lower or "lumber" in problem_lower:
        return "lumberjacking"
    if "pickaxe" in problem_lower or "mining" in problem_lower:
        return "mining"
    if "ingot" in problem_lower or "smelt" in problem_lower:
        return "blacksmithy"
    if "bandage" in problem_lower or "heal" in problem_lower:
        return "healing"
    # Fallback: use first few meaningful words
    words = [w for w in problem_lower.split() if len(w) > 3]
    return " ".join(words[:3]) if words else "guide"


async def forum_write_action(ctx: BrainContext) -> "Status":
    """Compose and post a forum entry about recent adventures (markdown)."""
    from anima.brain.behavior_tree import Status

    forum: ForumClient | None = ctx.blackboard.get("forum_client")
    if forum is None or ctx.memory_db is None or ctx.llm is None:
        return Status.FAILURE

    memory_db = ctx.memory_db
    agent_name = _agent_name(ctx)
    now = time.time()
    last_post = ctx.blackboard.get("forum_last_post", 0.0)
    post_interval = ctx.cfg.forum.post_interval

    if now - last_post < post_interval:
        return Status.FAILURE

    # Get recent episodes to summarize
    episodes = await memory_db.query_episodes(agent_name, limit=15)
    if len(episodes) < 3:
        return Status.FAILURE

    ep_lines = []
    for ep in episodes:
        line = f"- {ep.action}"
        if ep.target:
            line += f" → {ep.target}"
        if ep.outcome:
            line += f" ({ep.outcome})"
        ep_lines.append(line)

    # Build stats for context
    ss = ctx.perception.self_state
    stats = f"Gold: {ss.gold}gp, HP: {ss.hits}/{ss.hits_max}"
    if ss.weight_max > 0:
        stats += f", Weight: {ss.weight}/{ss.weight_max}"

    prompt = f"""\
Write a short forum post about your recent day in Britannia.
You are {agent_name}. Write in character — casual, personal, like a real player diary.

Current status: {stats}
Position: ({ss.x}, {ss.y})

Recent events:
{chr(10).join(ep_lines)}

Format rules:
- Write in **Markdown**
- Title should be catchy but short
- Body: 2-4 short paragraphs
- Include what you did, what went well, what went wrong
- End with what you plan to do next
- Stay in character

Reply format:
TITLE: <title>
BODY:
<markdown body>"""

    sys_msg = (
        f"You are {agent_name}, writing a diary-style forum post. "
        f"Write naturally in markdown. Keep it under 200 words."
    )
    result = await ctx.llm.chat([
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ])

    if not result.text:
        return Status.FAILURE

    title, body = _parse_forum_post(result.text, agent_name)
    post_id = await forum.create_post(title, body, "adventures")
    ctx.blackboard["forum_last_post"] = now

    logger.info("forum_posted", post_id=post_id, title=title)

    # Also send experience summary (non-LLM, structured data)
    if hasattr(forum, "send_experience"):
        success_count = sum(1 for ep in episodes if ep.outcome == "success")
        await forum.send_experience(
            exp_type="daily_summary",
            summary=f"{agent_name}: {success_count}/{len(episodes)} successful actions",
            location=f"({ss.x}, {ss.y})",
            gold_delta=0,
            notable=success_count > 5,
        )

    return Status.SUCCESS


def _parse_forum_post(text: str, fallback_author: str) -> tuple[str, str]:
    """Parse TITLE:/BODY: format from LLM output."""
    title = f"{fallback_author}'s Adventures"
    body = text

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.upper().startswith("TITLE:"):
            title = line[6:].strip()
        elif line.upper().startswith("BODY:"):
            # Body is everything after the BODY: line
            rest = line[5:].strip()
            if rest:
                body = rest + "\n" + "\n".join(lines[i + 1:])
            else:
                body = "\n".join(lines[i + 1:])
            body = body.strip()
            break

    return title, body


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"
