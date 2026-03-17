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
    """Read new forum posts and add useful info to knowledge."""
    from anima.brain.behavior_tree import Status

    forum: ForumClient | None = ctx.blackboard.get("forum_client")
    if forum is None or ctx.memory_db is None:
        return Status.FAILURE

    memory_db = ctx.memory_db
    agent_name = _agent_name(ctx)
    now = time.time()
    last_read = ctx.blackboard.get("forum_last_read", 0.0)
    read_interval = ctx.cfg.forum.read_interval

    if now - last_read < read_interval:
        return Status.FAILURE

    ctx.blackboard["forum_last_read"] = now

    posts = await forum.read_posts("adventures", limit=5)
    new_facts = 0
    for post in posts:
        if post.author == agent_name:
            continue
        # Add post content as knowledge from forum
        fact = f"{post.author} wrote: {post.title}"
        await memory_db.add_knowledge(
            agent_name, fact, source=f"forum:{post.author}", confidence=0.3
        )
        new_facts += 1

    if new_facts:
        logger.info("forum_read", new_facts=new_facts)

    return Status.SUCCESS


async def forum_write_action(ctx: BrainContext) -> "Status":
    """Compose and post a forum entry about recent adventures."""
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
    episodes = await memory_db.query_episodes(agent_name, limit=10)
    if len(episodes) < 3:
        return Status.FAILURE

    ep_lines = []
    for ep in episodes:
        line = f"- {ep.action}"
        if ep.target:
            line += f" at {ep.target}"
        line += f": {ep.outcome}" if ep.outcome else ""
        ep_lines.append(line)

    prompt = f"""\
Summarize these recent adventures for a forum post. Write as {agent_name},
a player character sharing their day in Britannia. Keep it short and interesting.

Recent events:
{chr(10).join(ep_lines)}

Write a short title and body. Format:
TITLE: <title>
BODY: <body>"""

    sys_msg = f"You are {agent_name}, writing a forum post about your adventures."
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
    return Status.SUCCESS


def _parse_forum_post(text: str, fallback_author: str) -> tuple[str, str]:
    """Parse TITLE:/BODY: format from LLM output."""
    title = f"{fallback_author}'s Adventures"
    body = text

    for line in text.splitlines():
        if line.upper().startswith("TITLE:"):
            title = line[6:].strip()
        elif line.upper().startswith("BODY:"):
            body = text[text.index(line) + len("BODY:"):].strip()
            break

    return title, body


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"
