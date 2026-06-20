"""update_action_stats accumulates into ONE row per (agent, context, action).

The RL reward buckets the LLM reads back via the "Past experience" block are
keyed by (agent_name, context_pattern, action). Before the fix the writer did a
SELECT-then-INSERT/UPDATE with no UNIQUE constraint, so two concurrent outcomes
for the same key could each read "no existing row" and both INSERT — leaving
duplicate rows that get_action_stats returns as split, double-counted stats
(one action appearing twice, its tallies and reward halved across rows).

The accessor is async and the runtime records combat/gather/craft outcomes
rapidly, so this raced in practice. The fix makes the index UNIQUE and the
write an atomic ON CONFLICT upsert, matching update_q_value /
update_location_value. These tests use in-memory SQLite.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.memory.database import MemoryDB


@pytest.fixture
async def db():
    mem = MemoryDB(":memory:")
    await mem.init()
    yield mem
    await mem.close()


class TestActionStatsAtomicUpsert:
    @pytest.mark.asyncio
    async def test_concurrent_outcomes_collapse_to_one_row(self, db: MemoryDB) -> None:
        # Fire many outcomes for the SAME key concurrently. With the old
        # read-modify-write + non-unique index this raced into duplicate rows;
        # the atomic upsert keeps exactly one and never raises a UNIQUE error.
        await asyncio.gather(
            *(
                db.update_action_stats("Anima", "exploring", "hunt", success=True, reward=2.0)
                for _ in range(40)
            )
        )
        rows = await db.db.execute_fetchall(
            """SELECT successes, failures, total_reward FROM action_stats
               WHERE agent_name = ? AND context_pattern = ? AND action = ?""",
            ("Anima", "exploring", "hunt"),
        )
        # Exactly one accumulated bucket — not several split duplicates.
        assert len(rows) == 1
        assert rows[0]["successes"] == 40
        assert rows[0]["failures"] == 0
        assert rows[0]["total_reward"] == pytest.approx(80.0)

    @pytest.mark.asyncio
    async def test_stats_accumulate_correctly(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "near_player", "speak", success=True, reward=3.0)
        await db.update_action_stats("Anima", "near_player", "speak", success=True, reward=1.0)
        await db.update_action_stats("Anima", "near_player", "speak", success=False, reward=-2.0)

        stats = await db.get_action_stats("Anima", "near_player")
        assert len(stats) == 1
        s = stats[0]
        assert s.successes == 2
        assert s.failures == 1
        assert s.total_reward == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_distinct_keys_stay_separate(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "exploring", "hunt", success=True, reward=5.0)
        await db.update_action_stats("Anima", "exploring", "gather", success=True, reward=1.0)
        await db.update_action_stats("Anima", "low_hp", "hunt", success=False, reward=-9.0)

        exploring = await db.get_action_stats("Anima", "exploring")
        assert {s.action for s in exploring} == {"hunt", "gather"}
        low_hp = await db.get_action_stats("Anima", "low_hp")
        assert len(low_hp) == 1 and low_hp[0].action == "hunt"
