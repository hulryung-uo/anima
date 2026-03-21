"""Forum skills — read and write posts to uotavern as game skills.

Registered as skills so Q-learning decides when to read/write:
- ForumPost: write diary/guide/trade posts
- ForumRead: search library for knowledge when problems arise
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from anima.skills.base import Skill, SkillResult
from anima.skills.forum import ForumClient

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Post types with board mapping and prompt templates
POST_TYPES = {
    "diary": {
        "board": "tavern",
        "description": "Share your daily adventures",
        "system": (
            "You are {name}, writing a diary-style forum post. "
            "Write naturally in markdown. Keep it under 200 words."
        ),
        "prompt": """\
Write a short forum post about your recent adventures in Britannia.
You are {name}. Write in character — casual, personal.

Current status: {stats}

Recent events:
{events}

Format: Markdown. Title should be catchy but short.
Include what you did, what went well, what went wrong.
End with what you plan to do next. Stay in character.

Reply format:
TITLE: <title>
BODY:
<markdown body>""",
    },
    "guide": {
        "board": "library",
        "description": "Share knowledge or tips you learned",
        "system": (
            "You are {name}, writing a practical guide based on your experience. "
            "Write in markdown with clear sections. Keep it under 300 words."
        ),
        "prompt": """\
Write a short guide or tip based on what you've learned recently.
You are {name}, an experienced {persona} in Britannia.

Things you've learned:
{knowledge}

Skills you practice: {skills}

Format: Markdown with headers. Be practical and specific.
Include numbers, locations, or item names when possible.

Reply format:
TITLE: <title>
BODY:
<markdown body>""",
    },
    "trade": {
        "board": "trade",
        "description": "Post a trade offer or request",
        "system": (
            "You are {name}, posting on a trade board. "
            "Be brief and specific. Use markdown."
        ),
        "prompt": """\
Write a short trade post. You are {name}.

Your inventory: {inventory}
Gold: {gold}gp
Skills: {skills}

What can you offer or what do you need?
Be specific about items, quantities, and prices.

Reply format:
TITLE: <title>
BODY:
<markdown body>""",
    },
}


def _parse_post(text: str, fallback_author: str) -> tuple[str, str]:
    """Parse TITLE:/BODY: format from LLM output."""
    title = f"{fallback_author}'s Post"
    body = text

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.upper().startswith("TITLE:"):
            title = line[6:].strip()
        elif line.upper().startswith("BODY:"):
            rest = line[5:].strip()
            if rest:
                body = rest + "\n" + "\n".join(lines[i + 1:])
            else:
                body = "\n".join(lines[i + 1:])
            body = body.strip()
            break

    return title, body


class ForumPost(Skill):
    """Write a forum post — diary, guide, or trade."""

    name = "forum_post"
    category = "social"
    description = "Write a forum post sharing adventures, knowledge, or trade offers."

    async def can_execute(self, ctx: BrainContext) -> bool:
        forum: ForumClient | None = ctx.blackboard.get("forum_client")
        if forum is None or ctx.memory_db is None or ctx.llm is None:
            return False

        now = time.time()
        last_post = ctx.blackboard.get("forum_last_post", 0.0)
        post_interval = ctx.cfg.forum.post_interval
        return now - last_post >= post_interval

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        forum: ForumClient | None = ctx.blackboard.get("forum_client")
        if forum is None or ctx.memory_db is None or ctx.llm is None:
            return SkillResult(success=False, reward=-1.0, message="No forum client")

        agent_name = _agent_name(ctx)
        memory_db = ctx.memory_db
        ss = ctx.perception.self_state

        # Decide post type based on context
        post_type = self._choose_post_type(ctx)
        template = POST_TYPES[post_type]

        # Build context for prompt
        episodes = await memory_db.query_episodes(agent_name, limit=15)
        ep_lines = []
        for ep in episodes:
            line = f"- {ep.action}"
            if ep.target:
                line += f" → {ep.target}"
            if ep.outcome:
                line += f" ({ep.outcome})"
            ep_lines.append(line)

        stats = f"Gold: {ss.gold}gp, HP: {ss.hits}/{ss.hits_max}"
        if ss.weight_max > 0:
            stats += f", Weight: {ss.weight}/{ss.weight_max}"

        # Get knowledge for guide posts
        knowledge_items = await memory_db.query_knowledge(agent_name, limit=5)
        knowledge = "\n".join(
            f"- {k.fact}" for k in knowledge_items
        ) if knowledge_items else "Nothing specific yet."

        # Get skills for context
        skill_names = []
        for sk in sorted(ss.skills.values(), key=lambda s: -s.value):
            if sk.value > 10:
                skill_names.append(f"{sk.id}:{sk.value:.0f}")
        skills_str = ", ".join(skill_names[:5]) or "beginner"

        # Get inventory for trade posts
        backpack = ss.equipment.get(0x15)
        inv_items = []
        if backpack:
            from anima.data import item_name
            for it in ctx.perception.world.items.values():
                if it.container == backpack:
                    name = it.name or item_name(it.graphic)
                    if name and it.amount > 1:
                        inv_items.append(f"{name} x{it.amount}")
                    elif name:
                        inv_items.append(name)
        inventory = ", ".join(inv_items[:10]) or "mostly empty"

        persona = ctx.blackboard.get("persona")
        persona_type = persona.title if persona else "adventurer"

        # Format prompt
        prompt = template["prompt"].format(
            name=agent_name,
            stats=stats,
            events="\n".join(ep_lines),
            knowledge=knowledge,
            skills=skills_str,
            inventory=inventory,
            gold=ss.gold,
            persona=persona_type,
        )
        sys_msg = template["system"].format(name=agent_name)

        result = await ctx.llm.chat([
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ])

        if not result.text:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-0.5,
                message="LLM returned empty", duration_ms=elapsed,
            )

        title, body = _parse_post(result.text, agent_name)
        post_id = await forum.create_post(title, body, template["board"])
        ctx.blackboard["forum_last_post"] = time.time()

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "forum_skill_posted",
            type=post_type, post_id=post_id,
            title=title, board=template["board"],
        )

        if post_id:
            return SkillResult(
                success=True, reward=1.0,  # low reward — posting is secondary to gameplay
                message=f"Posted [{post_type}]: {title}",
                duration_ms=elapsed,
            )
        return SkillResult(
            success=False, reward=-1.0,
            message="Post failed", duration_ms=elapsed,
        )

    def _choose_post_type(self, ctx: BrainContext) -> str:
        """Choose post type based on context."""
        last_types: list[str] = ctx.blackboard.get("_forum_post_types", [])

        # Rotate: diary → guide → trade → diary...
        if not last_types or last_types[-1] == "trade":
            choice = "diary"
        elif last_types[-1] == "diary":
            choice = "guide"
        else:
            choice = "trade"

        last_types.append(choice)
        if len(last_types) > 10:
            last_types = last_types[-10:]
        ctx.blackboard["_forum_post_types"] = last_types

        return choice


class ForumRead(Skill):
    """Read forum library for knowledge — triggered by problems or periodically."""

    name = "forum_read"
    category = "social"
    description = "Search the forum library for guides and knowledge."

    async def can_execute(self, ctx: BrainContext) -> bool:
        forum: ForumClient | None = ctx.blackboard.get("forum_client")
        if forum is None or ctx.memory_db is None:
            return False

        # Read when there's a problem to research
        if ctx.blackboard.get("skill_problem"):
            return True

        # Or periodically
        now = time.time()
        last_read = ctx.blackboard.get("forum_last_read", 0.0)
        return now - last_read >= ctx.cfg.forum.read_interval

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        forum: ForumClient | None = ctx.blackboard.get("forum_client")
        if forum is None or ctx.memory_db is None:
            return SkillResult(success=False, reward=-1.0, message="No forum")

        agent_name = _agent_name(ctx)
        memory_db = ctx.memory_db

        # Consume problem if any
        problem = ctx.blackboard.pop("skill_problem", None)

        if problem:
            query = _extract_search_query(problem)
            logger.info("forum_research", query=query, problem=problem[:60])
            posts = await forum.search(query)
            if not posts:
                posts = await forum.read_posts("library", limit=10)
        else:
            posts = await forum.read_posts("library", limit=5)

        ctx.blackboard["forum_last_read"] = time.time()

        new_facts = 0
        for post in posts[:5]:
            if post.author == agent_name:
                continue
            snippet = post.body[:200].replace("\n", " ").strip()
            fact = f"[Library] {post.title}: {snippet}"
            await memory_db.add_knowledge(
                agent_name, fact,
                source=f"library:{post.post_id[:8]}",
                confidence=0.7,
            )
            new_facts += 1

        elapsed = (time.monotonic() - start) * 1000

        if new_facts:
            logger.info("forum_learned", facts=new_facts, research=bool(problem))
            return SkillResult(
                success=True, reward=1.0,
                message=f"Learned {new_facts} facts from library",
                duration_ms=elapsed,
            )
        return SkillResult(
            success=False, reward=-0.5,
            message="Nothing new in library",
            duration_ms=elapsed,
        )


def _extract_search_query(problem: str) -> str:
    """Extract a search query from a problem description."""
    problem_lower = problem.lower()
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
    words = [w for w in problem_lower.split() if len(w) > 3]
    return " ".join(words[:3]) if words else "guide"


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"
