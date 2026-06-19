"""Re-observed knowledge facts confirm an existing row, never duplicate it.

Both the reflection loop (every 20 episodes) and the discovery scanner
(re-scans the same vendors/resources each pass) call ``add_knowledge`` with
identical fact strings repeatedly. Before the fix every call INSERTed a fresh
row, so the ``query_knowledge`` top-N filled with copies of one fact and
crowded out everything else the agent knew — recall collapsed to a single
repeated line, and re-seeing a fact never raised confidence.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.memory.database import MemoryDB


@pytest.fixture
async def db(tmp_path):
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


class TestKnowledgeDedup:
    @pytest.mark.asyncio
    async def test_repeat_fact_does_not_duplicate(self, db: MemoryDB) -> None:
        f = "The bank is at (1434, 1699)"
        id1 = await db.add_knowledge("Anima", f, "exploration", 0.5)
        id2 = await db.add_knowledge("Anima", f, "exploration", 0.5)
        id3 = await db.add_knowledge("Anima", f, "exploration", 0.5)
        # Every re-observation maps to the same row.
        assert id1 == id2 == id3
        rows = await db.db.execute_fetchall(
            "SELECT * FROM knowledge WHERE agent_name = ? AND fact = ?", ("Anima", f)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_repeat_confirms_confidence(self, db: MemoryDB) -> None:
        f = "hulryung speaks Korean and is friendly"
        await db.add_knowledge("Anima", f, "reflection", 0.5)
        await db.add_knowledge("Anima", f, "reflection", 0.5)
        facts = await db.query_knowledge("Anima")
        assert len(facts) == 1
        # Re-observation strengthens confidence by the confirm_knowledge step.
        assert facts[0].confidence == pytest.approx(0.6, abs=0.01)

    @pytest.mark.asyncio
    async def test_distinct_facts_not_crowded_out(self, db: MemoryDB) -> None:
        repeated = "Walking near (1550, 1620) often gets blocked"
        # The reflection loop re-extracts the same observation many times.
        for _ in range(6):
            await db.add_knowledge("Anima", repeated, "reflection", 0.5)
        await db.add_knowledge("Anima", "The forge is in Minoc", "exploration", 0.55)
        facts = await db.query_knowledge("Anima", limit=5)
        sentences = {k.fact for k in facts}
        # The single distinct fact survives — it is not buried under 6 copies.
        assert "The forge is in Minoc" in sentences
        assert len(facts) == 2

    @pytest.mark.asyncio
    async def test_concurrent_repeat_no_duplicate_rows(self, db: MemoryDB) -> None:
        f = "ore is plentiful near (2500, 560)"
        await asyncio.gather(
            *[db.add_knowledge("Anima", f, "exploration", 0.5) for _ in range(5)]
        )
        rows = await db.db.execute_fetchall(
            "SELECT * FROM knowledge WHERE agent_name = ? AND fact = ?", ("Anima", f)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_confidence_never_weakened_by_low_readd(self, db: MemoryDB) -> None:
        f = "the smith sells ingots"
        await db.add_knowledge("Anima", f, "exploration", 0.9)
        # A later low-confidence re-add must not drag the stored value down.
        await db.add_knowledge("Anima", f, "reflection", 0.3)
        facts = await db.query_knowledge("Anima")
        assert len(facts) == 1
        assert facts[0].confidence == pytest.approx(1.0, abs=0.01)
