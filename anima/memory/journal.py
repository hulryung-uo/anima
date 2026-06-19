"""Activity journal — narrative log of agent actions for essays and forum posts.

The journal records agent activities as human-readable narrative entries,
richer than the structured ``episodes`` table.  Entries are stored in SQLite
and can be compiled into cohesive stories for forum posts or self-reflection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.memory.database import MemoryDB
    from anima.skills.base import SkillResult

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Schema (added to existing DB via ensure_table)
# ---------------------------------------------------------------------------

_JOURNAL_SCHEMA = """\
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    location_x INTEGER NOT NULL DEFAULT 0,
    location_y INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    narrative TEXT NOT NULL,
    mood TEXT NOT NULL DEFAULT 'neutral',
    importance INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_journal_agent_time
    ON journal(agent_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_journal_category
    ON journal(agent_name, category);
"""

_MIGRATE_ADD_TITLE = """\
ALTER TABLE journal ADD COLUMN title TEXT NOT NULL DEFAULT '';
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class JournalEntry:
    """A single narrative journal entry."""

    id: int = 0
    agent_name: str = ""
    timestamp: float = field(default_factory=time.time)
    location_x: int = 0
    location_y: int = 0
    category: str = ""  # "crafting", "gathering", "combat", "trade", "social", "exploration"
    action: str = ""  # skill name or event type
    title: str = ""  # short title for the entry
    narrative: str = ""  # human-readable narrative text
    mood: str = "neutral"  # "satisfied", "frustrated", "excited", "neutral"
    importance: int = 1  # 1=routine, 2=notable, 3=significant


# ---------------------------------------------------------------------------
# Narrative generators
# ---------------------------------------------------------------------------

# Mapping of skill names to narrative templates
# Each entry has: title (short label), success/failure narratives
_SKILL_NARRATIVES: dict[str, dict[str, str]] = {
    "chop_wood": {
        "title_success": "벌목 성공",
        "title_failure": "벌목 실패",
        "success": "{name}은(는) 나무를 벌목하여 통나무를 얻었다. {detail}",
        "failure": "{name}은(는) 나무를 벌목하려 했지만 실패했다. {detail}",
    },
    "mine_ore": {
        "title_success": "채광 성공",
        "title_failure": "채광 실패",
        "success": "{name}은(는) 광석을 캐내는 데 성공했다. {detail}",
        "failure": "{name}은(는) 곡괭이를 휘둘렀지만 아무것도 캐지 못했다. {detail}",
    },
    "smelt_ore": {
        "title_success": "제련 완료",
        "title_failure": "제련 실패",
        "success": "{name}은(는) 광석을 제련하여 주괴를 만들었다. {detail}",
        "failure": "{name}은(는) 광석을 제련하려 했지만 실패했다. {detail}",
    },
    "craft_tinker": {
        "title_success": "팅커링 제작",
        "title_failure": "팅커링 실패",
        "success": "{name}은(는) 팅커링으로 도구를 만들었다. {detail}",
        "failure": "{name}은(는) 도구를 만들려 했으나 실패했다. {detail}",
    },
    "craft_carpentry": {
        "title_success": "목공 제작",
        "title_failure": "목공 실패",
        "success": "{name}은(는) 목공으로 {detail}을(를) 제작했다.",
        "failure": "{name}은(는) 목공 작업에 실패했다. 재료가 낭비되었다. {detail}",
    },
    "craft_blacksmith": {
        "title_success": "대장장이 제작",
        "title_failure": "대장장이 실패",
        "success": "{name}은(는) 대장장이 기술로 {detail}을(를) 제작했다.",
        "failure": "{name}은(는) 대장장이 작업에 실패했다. {detail}",
    },
    "sell_to_npc": {
        "title_success": "물건 판매",
        "title_failure": "판매 실패",
        "success": "{name}은(는) 상인에게 물건을 팔았다. {detail}",
        "failure": "{name}은(는) 물건을 팔려 했지만 실패했다. {detail}",
    },
    "buy_from_npc": {
        "title_success": "물건 구매",
        "title_failure": "구매 실패",
        "success": "{name}은(는) 상인에게서 물건을 구입했다. {detail}",
        "failure": "{name}은(는) 물건을 사려 했지만 실패했다. {detail}",
    },
    "melee_attack": {
        "title_success": "전투 승리",
        "title_failure": "전투 고전",
        "success": "{name}은(는) 전투에서 적을 쓰러뜨렸다. {detail}",
        "failure": "{name}은(는) 전투에서 고전했다. {detail}",
    },
    "heal_self": {
        "title_success": "치료 성공",
        "title_failure": "치료 실패",
        "success": "{name}은(는) 붕대로 상처를 치료했다. {detail}",
        "failure": "{name}은(는) 치료를 시도했지만 효과가 없었다. {detail}",
    },
}


def build_narrative(
    agent_name: str,
    skill_name: str,
    result: SkillResult,
) -> tuple[str, str]:
    """Generate a (title, narrative) tuple from a skill execution result.

    Returns (title, narrative) where title is a short label and narrative
    is the full descriptive text including detail from the result.
    """
    detail = result.message or ""
    templates = _SKILL_NARRATIVES.get(skill_name)
    if templates:
        key = "success" if result.success else "failure"
        title_key = f"title_{key}"
        title = templates.get(title_key, skill_name)
        template = templates.get(key, "{name}은(는) {action}을(를) 수행했다.")
        narrative = template.format(
            name=agent_name,
            action=skill_name,
            detail=detail,
        ).strip()
        # For notable events, append extra context if detail is present
        if detail and result.reward >= 3.0:
            title = f"{title} — {detail[:40]}"
        return title, narrative
    # Fallback for unknown skills
    if result.success:
        title = f"{skill_name} 성공"
        narrative = f"{agent_name}은(는) {skill_name}을(를) 성공적으로 수행했다. {detail}"
    else:
        title = f"{skill_name} 실패"
        narrative = f"{agent_name}은(는) {skill_name}에 실패했다. {detail}"
    if detail and result.reward >= 3.0:
        title = f"{title} — {detail[:40]}"
    return title, narrative.strip()


def result_to_mood(result: SkillResult) -> str:
    """Infer mood from a skill result."""
    if result.success and result.reward >= 5.0:
        return "excited"
    if result.success:
        return "satisfied"
    if result.reward <= -3.0:
        return "frustrated"
    return "neutral"


def result_to_importance(result: SkillResult) -> int:
    """Determine importance level of an event."""
    # Significance scales with the *magnitude* of the reward signal, in either
    # direction. A catastrophic outcome (large negative reward — e.g. a death or
    # heavy damage) is just as memorable as a windfall, so it must not be filed
    # as routine noise that ``recent_entries(min_importance=2)`` silently drops.
    # Thresholds mirror the positive side and align with ``result_to_mood``'s
    # -3.0 "frustrated" cutoff.
    if result.reward <= -8.0:
        return 3  # significant (catastrophic)
    if result.reward >= 8.0:
        return 3  # significant
    if result.success and result.reward >= 3.0:
        return 2  # notable
    if result.reward <= -3.0:
        return 2  # notable (painful)
    return 1  # routine


# ---------------------------------------------------------------------------
# Journal writer
# ---------------------------------------------------------------------------


class ActivityJournal:
    """Writes and queries narrative journal entries in SQLite."""

    def __init__(self, memory_db: MemoryDB, agent_name: str = "Anima") -> None:
        self._db = memory_db
        self._agent_name = agent_name
        self._initialized = False

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        await self._db.db.executescript(_JOURNAL_SCHEMA)
        # Migrate: add title column if missing (existing DBs)
        try:
            await self._db.db.execute(_MIGRATE_ADD_TITLE)
        except Exception:
            pass  # column already exists
        await self._db.db.commit()
        self._initialized = True

    async def record_skill(
        self,
        skill_name: str,
        result: SkillResult,
        x: int = 0,
        y: int = 0,
    ) -> JournalEntry:
        """Record a skill execution as a narrative journal entry."""
        await self._ensure_table()

        title, narrative = build_narrative(self._agent_name, skill_name, result)
        mood = result_to_mood(result)
        importance = result_to_importance(result)
        category = _skill_to_category(skill_name)

        entry = JournalEntry(
            agent_name=self._agent_name,
            location_x=x,
            location_y=y,
            category=category,
            action=skill_name,
            title=title,
            narrative=narrative,
            mood=mood,
            importance=importance,
        )

        cursor = await self._db.db.execute(
            """INSERT INTO journal
               (agent_name, timestamp, location_x, location_y,
                category, action, title, narrative, mood, importance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.agent_name,
                entry.timestamp,
                entry.location_x,
                entry.location_y,
                entry.category,
                entry.action,
                entry.title,
                entry.narrative,
                entry.mood,
                entry.importance,
            ),
        )
        await self._db.db.commit()
        entry.id = cursor.lastrowid  # type: ignore[assignment]

        logger.debug("journal_entry", title=title, narrative=narrative, mood=mood, importance=importance)
        return entry

    async def record_event(
        self,
        narrative: str,
        category: str = "event",
        action: str = "",
        title: str = "",
        x: int = 0,
        y: int = 0,
        mood: str = "neutral",
        importance: int = 1,
    ) -> JournalEntry:
        """Record a freeform event as a journal entry."""
        await self._ensure_table()

        entry = JournalEntry(
            agent_name=self._agent_name,
            location_x=x,
            location_y=y,
            category=category,
            action=action,
            title=title or action or category,
            narrative=narrative,
            mood=mood,
            importance=importance,
        )

        cursor = await self._db.db.execute(
            """INSERT INTO journal
               (agent_name, timestamp, location_x, location_y,
                category, action, title, narrative, mood, importance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.agent_name,
                entry.timestamp,
                entry.location_x,
                entry.location_y,
                entry.category,
                entry.action,
                entry.title,
                entry.narrative,
                entry.mood,
                entry.importance,
            ),
        )
        await self._db.db.commit()
        entry.id = cursor.lastrowid  # type: ignore[assignment]
        return entry

    async def recent_entries(
        self,
        limit: int = 20,
        category: str | None = None,
        min_importance: int = 1,
    ) -> list[JournalEntry]:
        """Get recent journal entries."""
        await self._ensure_table()

        conditions = ["agent_name = ?", "importance >= ?"]
        params: list = [self._agent_name, min_importance]

        if category:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await self._db.db.execute_fetchall(
            f"""SELECT * FROM journal
                WHERE {where}
                ORDER BY timestamp DESC LIMIT ?""",
            params,
        )
        return [_row_to_journal(r) for r in reversed(rows)]  # chronological order

    async def compile_narrative(
        self,
        hours: float = 24.0,
        min_importance: int = 1,
    ) -> str:
        """Compile recent journal entries into a cohesive narrative.

        Returns a multi-paragraph text suitable for forum posts or essays.
        Each category section gets a heading, and notable entries are
        highlighted with their titles and full detail.
        """
        await self._ensure_table()

        cutoff = time.time() - hours * 3600
        rows = await self._db.db.execute_fetchall(
            """SELECT * FROM journal
               WHERE agent_name = ? AND timestamp >= ? AND importance >= ?
               ORDER BY timestamp ASC""",
            (self._agent_name, cutoff, min_importance),
        )

        if not rows:
            return ""

        entries = [_row_to_journal(r) for r in rows]

        _CATEGORY_TITLES = {
            "gathering": "자원 채집",
            "crafting": "제작 활동",
            "trade": "거래",
            "combat": "전투",
            "social": "사회 활동",
            "exploration": "탐험",
            "thinking": "생각",
            "event": "사건",
            "activity": "활동",
        }

        # Group by category for coherent sections. Entries arrive in timestamp
        # order, so the same category can recur interleaved with others (chop →
        # craft → chop). Coalescing only *consecutive* runs would emit the same
        # category header twice and scatter that activity across the essay; a
        # true group collects every entry of a category under one heading. Use a
        # dict, which preserves first-appearance (insertion) order, so the
        # section order still tracks when each activity was first seen.
        grouped: dict[str, list[str]] = {}
        for entry in entries:
            # Notable entries (importance >= 2) get their title as emphasis
            if entry.importance >= 2 and entry.title:
                line = f"**{entry.title}** — {entry.narrative}"
            else:
                line = entry.narrative
            grouped.setdefault(entry.category, []).append(line)

        sections: list[str] = []
        for category, lines in grouped.items():
            cat_title = _CATEGORY_TITLES.get(category, category)
            sections.append(f"### {cat_title}\n\n" + "\n".join(lines))

        return "\n\n".join(sections)

    async def summarize_day(self) -> dict[str, int]:
        """Get a summary of today's activities by category."""
        await self._ensure_table()

        today_start = time.time() - 24 * 3600
        rows = await self._db.db.execute_fetchall(
            """SELECT category, COUNT(*) as cnt FROM journal
               WHERE agent_name = ? AND timestamp >= ?
               GROUP BY category""",
            (self._agent_name, today_start),
        )
        return {r["category"]: r["cnt"] for r in rows}

    async def prune(self, max_entries: int = 1000) -> int:
        """Evict the *least valuable* entries once the agent passes max_entries.

        Pruning purely by ``timestamp ASC`` discards the oldest rows regardless
        of how memorable they are — so the rare ``importance=3`` events (a death,
        a windfall, a first sighting; the very ones ``result_to_importance``
        deliberately marks so they survive ``recent_entries(min_importance=2)``)
        get thrown away while recent routine ``importance=1`` noise is kept.
        That silently erodes exactly the memories that drive the forum essays
        and self-reflection ``compile_narrative`` reads.

        Evict by ``importance ASC, timestamp ASC`` instead: the cheapest, oldest
        entries go first and significant memories are protected from the cap.
        """
        await self._ensure_table()

        cursor = await self._db.db.execute(
            "SELECT COUNT(*) FROM journal WHERE agent_name = ?",
            (self._agent_name,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0

        if count <= max_entries:
            return 0

        to_delete = count - max_entries
        await self._db.db.execute(
            """DELETE FROM journal WHERE id IN (
                 SELECT id FROM journal WHERE agent_name = ?
                 ORDER BY importance ASC, timestamp ASC LIMIT ?
               )""",
            (self._agent_name, to_delete),
        )
        await self._db.db.commit()
        return to_delete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_to_category(skill_name: str) -> str:
    """Map skill name to journal category."""
    mapping = {
        "chop_wood": "gathering",
        "mine_ore": "gathering",
        "smelt_ore": "crafting",
        "craft_tinker": "crafting",
        "craft_carpentry": "crafting",
        "craft_blacksmith": "crafting",
        "sell_to_npc": "trade",
        "buy_from_npc": "trade",
        "melee_attack": "combat",
        "heal_self": "combat",
    }
    return mapping.get(skill_name, "activity")


def _row_to_journal(row) -> JournalEntry:
    return JournalEntry(
        id=row["id"],
        agent_name=row["agent_name"],
        timestamp=row["timestamp"],
        location_x=row["location_x"],
        location_y=row["location_y"],
        category=row["category"],
        action=row["action"],
        title=row["title"] if "title" in row.keys() else "",
        narrative=row["narrative"],
        mood=row["mood"],
        importance=row["importance"],
    )
