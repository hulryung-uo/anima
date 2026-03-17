"""Async SQLite wrapper for persistent memory storage."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Episode:
    id: int
    agent_name: str
    timestamp: float
    location_x: int
    location_y: int
    action: str
    target: str
    outcome: str
    reward: float
    context: dict
    summary: str


@dataclass
class Knowledge:
    id: int
    agent_name: str
    fact: str
    source: str
    confidence: float
    created: float
    last_confirmed: float


@dataclass
class Relationship:
    id: int
    agent_name: str
    entity_serial: int
    entity_name: str
    disposition: float
    trust: float
    interaction_count: int
    last_interaction: float
    notes: dict


@dataclass
class ActionStat:
    id: int
    agent_name: str
    context_pattern: str
    action: str
    successes: int
    failures: int
    total_reward: float
    last_updated: float


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    location_x INTEGER NOT NULL,
    location_y INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    reward REAL NOT NULL DEFAULT 0.0,
    context TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    fact TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'experience',
    confidence REAL NOT NULL DEFAULT 0.5,
    created REAL NOT NULL,
    last_confirmed REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    entity_serial INTEGER NOT NULL,
    entity_name TEXT NOT NULL DEFAULT '',
    disposition REAL NOT NULL DEFAULT 0.0,
    trust REAL NOT NULL DEFAULT 0.5,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    last_interaction REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS action_stats (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    context_pattern TEXT NOT NULL,
    action TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    total_reward REAL NOT NULL DEFAULT 0.0,
    last_updated REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_agent ON episodes(agent_name);
CREATE INDEX IF NOT EXISTS idx_episodes_location ON episodes(location_x, location_y);
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_agent ON knowledge(agent_name);
CREATE INDEX IF NOT EXISTS idx_relationships_agent ON relationships(agent_name, entity_serial);
CREATE INDEX IF NOT EXISTS idx_action_stats_agent
    ON action_stats(agent_name, context_pattern, action);

CREATE TABLE IF NOT EXISTS q_values (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    state_key TEXT NOT NULL,
    action TEXT NOT NULL,
    q_value REAL NOT NULL DEFAULT 0.0,
    visit_count INTEGER NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_q_values_lookup
    ON q_values(agent_name, state_key, action);

CREATE TABLE IF NOT EXISTS location_values (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    region_x INTEGER NOT NULL,
    region_y INTEGER NOT NULL,
    activity TEXT NOT NULL,
    total_reward REAL NOT NULL DEFAULT 0.0,
    visit_count INTEGER NOT NULL DEFAULT 0,
    last_visited REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_location_values_lookup
    ON location_values(agent_name, region_x, region_y, activity);
"""


# ---------------------------------------------------------------------------
# MemoryDB
# ---------------------------------------------------------------------------


class MemoryDB:
    """Async SQLite-backed memory store."""

    def __init__(self, db_path: str | Path = "data/anima.db") -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the database and create tables if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("memory_db_ready", path=str(self.db_path))

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "MemoryDB not initialized — call init() first"
        return self._db

    # -------------------------------------------------------------------
    # Episodes
    # -------------------------------------------------------------------

    async def record_episode(
        self,
        agent_name: str,
        location_x: int,
        location_y: int,
        action: str,
        target: str = "",
        outcome: str = "",
        reward: float = 0.0,
        context: dict | None = None,
        summary: str = "",
    ) -> int:
        """Log an experience episode. Returns the episode ID."""
        now = time.time()
        ctx_json = json.dumps(context or {})
        cursor = await self.db.execute(
            """INSERT INTO episodes
               (agent_name, timestamp, location_x, location_y,
                action, target, outcome, reward, context, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_name,
                now,
                location_x,
                location_y,
                action,
                target,
                outcome,
                reward,
                ctx_json,
                summary,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def query_episodes(
        self,
        agent_name: str,
        location_x: int | None = None,
        location_y: int | None = None,
        action: str | None = None,
        limit: int = 5,
    ) -> list[Episode]:
        """Retrieve recent episodes, optionally filtered by location or action."""
        conditions = ["agent_name = ?"]
        params: list = [agent_name]

        if location_x is not None and location_y is not None:
            conditions.append("ABS(location_x - ?) + ABS(location_y - ?) < 50")
            params.extend([location_x, location_y])

        if action:
            conditions.append("action = ?")
            params.append(action)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await self.db.execute_fetchall(
            f"SELECT * FROM episodes WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        return [_row_to_episode(r) for r in rows]

    async def count_episodes(self, agent_name: str) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM episodes WHERE agent_name = ?", (agent_name,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def prune_episodes(self, agent_name: str, max_count: int) -> int:
        """Delete oldest episodes beyond max_count. Returns number deleted."""
        count = await self.count_episodes(agent_name)
        if count <= max_count:
            return 0
        to_delete = count - max_count
        await self.db.execute(
            """DELETE FROM episodes WHERE id IN (
                 SELECT id FROM episodes WHERE agent_name = ?
                 ORDER BY timestamp ASC LIMIT ?
               )""",
            (agent_name, to_delete),
        )
        await self.db.commit()
        return to_delete

    # -------------------------------------------------------------------
    # Knowledge
    # -------------------------------------------------------------------

    async def add_knowledge(
        self,
        agent_name: str,
        fact: str,
        source: str = "experience",
        confidence: float = 0.5,
    ) -> int:
        now = time.time()
        cursor = await self.db.execute(
            """INSERT INTO knowledge (agent_name, fact, source, confidence, created, last_confirmed)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_name, fact, source, confidence, now, now),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def query_knowledge(
        self, agent_name: str, keyword: str = "", limit: int = 5
    ) -> list[Knowledge]:
        if keyword:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM knowledge
                   WHERE agent_name = ? AND fact LIKE ?
                   ORDER BY confidence DESC, last_confirmed DESC LIMIT ?""",
                (agent_name, f"%{keyword}%", limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM knowledge
                   WHERE agent_name = ?
                   ORDER BY confidence DESC, last_confirmed DESC LIMIT ?""",
                (agent_name, limit),
            )
        return [_row_to_knowledge(r) for r in rows]

    async def confirm_knowledge(self, knowledge_id: int) -> None:
        """Bump confidence and update last_confirmed."""
        now = time.time()
        await self.db.execute(
            """UPDATE knowledge SET confidence = MIN(1.0, confidence + 0.1),
                                    last_confirmed = ?
               WHERE id = ?""",
            (now, knowledge_id),
        )
        await self.db.commit()

    # -------------------------------------------------------------------
    # Relationships
    # -------------------------------------------------------------------

    async def get_relationship(self, agent_name: str, entity_serial: int) -> Relationship | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM relationships WHERE agent_name = ? AND entity_serial = ?",
            (agent_name, entity_serial),
        )
        return _row_to_relationship(rows[0]) if rows else None

    async def update_relationship(
        self,
        agent_name: str,
        entity_serial: int,
        entity_name: str = "",
        disposition_delta: float = 0.0,
        trust_delta: float = 0.0,
        note: str = "",
    ) -> None:
        """Update or create a relationship with an entity."""
        now = time.time()
        existing = await self.get_relationship(agent_name, entity_serial)

        if existing:
            new_disp = max(-1.0, min(1.0, existing.disposition + disposition_delta))
            new_trust = max(0.0, min(1.0, existing.trust + trust_delta))
            new_count = existing.interaction_count + 1
            notes = existing.notes
            if note:
                notes_list = notes.get("interactions", [])
                notes_list.append({"time": now, "note": note})
                # Keep last 20 notes
                notes["interactions"] = notes_list[-20:]
            await self.db.execute(
                """UPDATE relationships
                   SET entity_name = COALESCE(NULLIF(?, ''), entity_name),
                       disposition = ?, trust = ?, interaction_count = ?,
                       last_interaction = ?, notes = ?
                   WHERE agent_name = ? AND entity_serial = ?""",
                (
                    entity_name,
                    new_disp,
                    new_trust,
                    new_count,
                    now,
                    json.dumps(notes),
                    agent_name,
                    entity_serial,
                ),
            )
        else:
            disp = max(-1.0, min(1.0, disposition_delta))
            trust = max(0.0, min(1.0, 0.5 + trust_delta))
            notes_dict: dict = {}
            if note:
                notes_dict["interactions"] = [{"time": now, "note": note}]
            await self.db.execute(
                """INSERT INTO relationships
                   (agent_name, entity_serial, entity_name, disposition, trust,
                    interaction_count, last_interaction, notes)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    agent_name,
                    entity_serial,
                    entity_name,
                    disp,
                    trust,
                    now,
                    json.dumps(notes_dict),
                ),
            )
        await self.db.commit()

    async def get_nearby_relationships(
        self, agent_name: str, serials: list[int]
    ) -> list[Relationship]:
        """Get relationships for a list of entity serials."""
        if not serials:
            return []
        placeholders = ",".join("?" for _ in serials)
        rows = await self.db.execute_fetchall(
            f"""SELECT * FROM relationships
                WHERE agent_name = ? AND entity_serial IN ({placeholders})""",
            [agent_name, *serials],
        )
        return [_row_to_relationship(r) for r in rows]

    # -------------------------------------------------------------------
    # Action stats
    # -------------------------------------------------------------------

    async def update_action_stats(
        self,
        agent_name: str,
        context_pattern: str,
        action: str,
        success: bool,
        reward: float = 0.0,
    ) -> None:
        """Record an action outcome for lightweight RL tracking."""
        now = time.time()
        rows = await self.db.execute_fetchall(
            """SELECT id, successes, failures, total_reward FROM action_stats
               WHERE agent_name = ? AND context_pattern = ? AND action = ?""",
            (agent_name, context_pattern, action),
        )
        if rows:
            row = rows[0]
            new_s = row["successes"] + (1 if success else 0)
            new_f = row["failures"] + (0 if success else 1)
            new_r = row["total_reward"] + reward
            await self.db.execute(
                """UPDATE action_stats SET successes = ?, failures = ?,
                                           total_reward = ?, last_updated = ?
                   WHERE id = ?""",
                (new_s, new_f, new_r, now, row["id"]),
            )
        else:
            await self.db.execute(
                """INSERT INTO action_stats
                   (agent_name, context_pattern, action,
                    successes, failures, total_reward, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_name,
                    context_pattern,
                    action,
                    1 if success else 0,
                    0 if success else 1,
                    reward,
                    now,
                ),
            )
        await self.db.commit()

    async def get_action_stats(self, agent_name: str, context_pattern: str) -> list[ActionStat]:
        """Get success rates for all actions in a given context."""
        rows = await self.db.execute_fetchall(
            """SELECT * FROM action_stats
               WHERE agent_name = ? AND context_pattern = ?
               ORDER BY total_reward DESC""",
            (agent_name, context_pattern),
        )
        return [_row_to_action_stat(r) for r in rows]

    async def get_all_action_stats(self, agent_name: str) -> list[ActionStat]:
        """Get all action stats for an agent."""
        rows = await self.db.execute_fetchall(
            "SELECT * FROM action_stats WHERE agent_name = ? ORDER BY total_reward DESC",
            (agent_name,),
        )
        return [_row_to_action_stat(r) for r in rows]

    # -------------------------------------------------------------------
    # Q-values
    # -------------------------------------------------------------------

    async def get_q_value(self, agent_name: str, state_key: str, action: str) -> float:
        """Get Q-value for a (state, action) pair. Returns 0.0 if not found."""
        cursor = await self.db.execute(
            """SELECT q_value FROM q_values
               WHERE agent_name = ? AND state_key = ? AND action = ?""",
            (agent_name, state_key, action),
        )
        row = await cursor.fetchone()
        return row["q_value"] if row else 0.0

    async def get_q_values(self, agent_name: str, state_key: str) -> dict[str, tuple[float, int]]:
        """Get all Q-values for a state. Returns {action: (q_value, visit_count)}."""
        rows = await self.db.execute_fetchall(
            """SELECT action, q_value, visit_count FROM q_values
               WHERE agent_name = ? AND state_key = ?""",
            (agent_name, state_key),
        )
        return {r["action"]: (r["q_value"], r["visit_count"]) for r in rows}

    async def update_q_value(
        self,
        agent_name: str,
        state_key: str,
        action: str,
        q_value: float,
        visit_count: int,
    ) -> None:
        """Upsert a Q-value entry."""
        now = time.time()
        await self.db.execute(
            """INSERT INTO q_values
               (agent_name, state_key, action, q_value, visit_count, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_name, state_key, action) DO UPDATE SET
                   q_value = excluded.q_value,
                   visit_count = excluded.visit_count,
                   last_updated = excluded.last_updated""",
            (agent_name, state_key, action, q_value, visit_count, now),
        )
        await self.db.commit()

    # -------------------------------------------------------------------
    # Location values
    # -------------------------------------------------------------------

    async def update_location_value(
        self,
        agent_name: str,
        region_x: int,
        region_y: int,
        activity: str,
        reward: float,
    ) -> None:
        """Record activity reward at a map region.

        Increments visit_count and adds to total_reward.
        """
        now = time.time()
        await self.db.execute(
            """INSERT INTO location_values
               (agent_name, region_x, region_y, activity, total_reward, visit_count, last_visited)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(agent_name, region_x, region_y, activity) DO UPDATE SET
                   total_reward = location_values.total_reward + excluded.total_reward,
                   visit_count = location_values.visit_count + 1,
                   last_visited = excluded.last_visited""",
            (agent_name, region_x, region_y, activity, reward, now),
        )
        await self.db.commit()

    async def get_location_values(
        self, agent_name: str, region_x: int, region_y: int
    ) -> list[tuple[str, float, int]]:
        """Get all activity values for a region.

        Returns [(activity, total_reward, visit_count), ...].
        """
        rows = await self.db.execute_fetchall(
            """SELECT activity, total_reward, visit_count FROM location_values
               WHERE agent_name = ? AND region_x = ? AND region_y = ?
               ORDER BY total_reward DESC""",
            (agent_name, region_x, region_y),
        )
        return [(r["activity"], r["total_reward"], r["visit_count"]) for r in rows]

    async def get_best_locations(
        self, agent_name: str, activity: str, limit: int = 5
    ) -> list[tuple[int, int, float, int]]:
        """Get top locations for an activity.

        Returns [(region_x, region_y, avg_reward, visit_count), ...].
        """
        rows = await self.db.execute_fetchall(
            """SELECT region_x, region_y,
                      total_reward / visit_count AS avg_reward,
                      visit_count
               FROM location_values
               WHERE agent_name = ? AND activity = ?
               ORDER BY avg_reward DESC
               LIMIT ?""",
            (agent_name, activity, limit),
        )
        return [(r["region_x"], r["region_y"], r["avg_reward"], r["visit_count"]) for r in rows]


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_episode(row: aiosqlite.Row) -> Episode:
    return Episode(
        id=row["id"],
        agent_name=row["agent_name"],
        timestamp=row["timestamp"],
        location_x=row["location_x"],
        location_y=row["location_y"],
        action=row["action"],
        target=row["target"],
        outcome=row["outcome"],
        reward=row["reward"],
        context=json.loads(row["context"]),
        summary=row["summary"],
    )


def _row_to_knowledge(row: aiosqlite.Row) -> Knowledge:
    return Knowledge(
        id=row["id"],
        agent_name=row["agent_name"],
        fact=row["fact"],
        source=row["source"],
        confidence=row["confidence"],
        created=row["created"],
        last_confirmed=row["last_confirmed"],
    )


def _row_to_relationship(row: aiosqlite.Row) -> Relationship:
    return Relationship(
        id=row["id"],
        agent_name=row["agent_name"],
        entity_serial=row["entity_serial"],
        entity_name=row["entity_name"],
        disposition=row["disposition"],
        trust=row["trust"],
        interaction_count=row["interaction_count"],
        last_interaction=row["last_interaction"],
        notes=json.loads(row["notes"]),
    )


def _row_to_action_stat(row: aiosqlite.Row) -> ActionStat:
    return ActionStat(
        id=row["id"],
        agent_name=row["agent_name"],
        context_pattern=row["context_pattern"],
        action=row["action"],
        successes=row["successes"],
        failures=row["failures"],
        total_reward=row["total_reward"],
        last_updated=row["last_updated"],
    )
