"""Tests for the persistent memory system."""

from __future__ import annotations

import pytest

from anima.memory.database import MemoryDB


@pytest.fixture
async def db(tmp_path):
    """Create an in-memory-like temp database for testing."""
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


# ---------------------------------------------------------------------------
# Episode tests
# ---------------------------------------------------------------------------


class TestEpisodes:
    @pytest.mark.asyncio
    async def test_record_and_query(self, db: MemoryDB) -> None:
        eid = await db.record_episode(
            agent_name="Anima",
            location_x=1000,
            location_y=2000,
            action="go",
            target="tavern",
            outcome="success",
            reward=10.0,
            summary="Arrived at tavern",
        )
        assert eid == 1

        episodes = await db.query_episodes("Anima", limit=5)
        assert len(episodes) == 1
        assert episodes[0].action == "go"
        assert episodes[0].target == "tavern"
        assert episodes[0].outcome == "success"
        assert episodes[0].reward == 10.0

    @pytest.mark.asyncio
    async def test_query_by_location(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Anima", 5000, 6000, "go", "dock", "success")

        # Should find only the nearby episode
        nearby = await db.query_episodes("Anima", location_x=1010, location_y=2010, limit=5)
        assert len(nearby) == 1
        assert nearby[0].target == "tavern"

    @pytest.mark.asyncio
    async def test_query_by_action(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Anima", 1000, 2000, "speak", "hello", "success")

        go_episodes = await db.query_episodes("Anima", action="go", limit=5)
        assert len(go_episodes) == 1
        assert go_episodes[0].action == "go"

    @pytest.mark.asyncio
    async def test_prune_episodes(self, db: MemoryDB) -> None:
        for i in range(10):
            await db.record_episode("Anima", 1000, 2000, "go", f"place_{i}", "success")

        pruned = await db.prune_episodes("Anima", max_count=5)
        assert pruned == 5
        assert await db.count_episodes("Anima") == 5

    @pytest.mark.asyncio
    async def test_agent_name_isolation(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Other", 1000, 2000, "go", "bank", "success")

        anima_eps = await db.query_episodes("Anima", limit=10)
        other_eps = await db.query_episodes("Other", limit=10)
        assert len(anima_eps) == 1
        assert len(other_eps) == 1
        assert anima_eps[0].target == "tavern"
        assert other_eps[0].target == "bank"


# ---------------------------------------------------------------------------
# Knowledge tests
# ---------------------------------------------------------------------------


class TestKnowledge:
    @pytest.mark.asyncio
    async def test_add_and_query(self, db: MemoryDB) -> None:
        kid = await db.add_knowledge("Anima", "The bank is at (1434, 1699)", "experience", 0.7)
        assert kid == 1

        facts = await db.query_knowledge("Anima", keyword="bank")
        assert len(facts) == 1
        assert "bank" in facts[0].fact
        assert facts[0].confidence == 0.7

    @pytest.mark.asyncio
    async def test_query_no_keyword(self, db: MemoryDB) -> None:
        await db.add_knowledge("Anima", "fact 1", "experience", 0.5)
        await db.add_knowledge("Anima", "fact 2", "experience", 0.8)

        facts = await db.query_knowledge("Anima")
        assert len(facts) == 2
        # Ordered by confidence desc
        assert facts[0].confidence == 0.8

    @pytest.mark.asyncio
    async def test_confirm_knowledge(self, db: MemoryDB) -> None:
        kid = await db.add_knowledge("Anima", "some fact", "experience", 0.5)
        await db.confirm_knowledge(kid)

        facts = await db.query_knowledge("Anima")
        assert facts[0].confidence == pytest.approx(0.6, abs=0.01)


# ---------------------------------------------------------------------------
# Relationship tests
# ---------------------------------------------------------------------------


class TestRelationships:
    @pytest.mark.asyncio
    async def test_create_relationship(self, db: MemoryDB) -> None:
        await db.update_relationship(
            "Anima", entity_serial=12345, entity_name="hulryung",
            disposition_delta=0.3, trust_delta=0.1, note="Friendly greeting",
        )
        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.entity_name == "hulryung"
        assert rel.disposition == pytest.approx(0.3)
        assert rel.trust == pytest.approx(0.6)  # 0.5 + 0.1
        assert rel.interaction_count == 1

    @pytest.mark.asyncio
    async def test_update_relationship(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 12345, "hulryung", 0.2, 0.1)
        await db.update_relationship("Anima", 12345, "hulryung", 0.1, 0.05)

        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.disposition == pytest.approx(0.3)
        assert rel.trust == pytest.approx(0.65)
        assert rel.interaction_count == 2

    @pytest.mark.asyncio
    async def test_disposition_clamped(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 12345, "hulryung", 0.9, 0.0)
        await db.update_relationship("Anima", 12345, "hulryung", 0.5, 0.0)

        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.disposition <= 1.0

    @pytest.mark.asyncio
    async def test_get_nearby_relationships(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 100, "alice", 0.2, 0.1)
        await db.update_relationship("Anima", 200, "bob", -0.1, 0.0)
        await db.update_relationship("Anima", 300, "charlie", 0.5, 0.3)

        rels = await db.get_nearby_relationships("Anima", [100, 300])
        assert len(rels) == 2
        names = {r.entity_name for r in rels}
        assert names == {"alice", "charlie"}

    @pytest.mark.asyncio
    async def test_get_nearby_empty(self, db: MemoryDB) -> None:
        rels = await db.get_nearby_relationships("Anima", [])
        assert rels == []


# ---------------------------------------------------------------------------
# Action stats tests
# ---------------------------------------------------------------------------


class TestActionStats:
    @pytest.mark.asyncio
    async def test_track_success(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "exploring", "go", success=True, reward=10.0)
        await db.update_action_stats("Anima", "exploring", "go", success=True, reward=5.0)
        await db.update_action_stats("Anima", "exploring", "go", success=False, reward=-5.0)

        stats = await db.get_action_stats("Anima", "exploring")
        assert len(stats) == 1
        assert stats[0].successes == 2
        assert stats[0].failures == 1
        assert stats[0].total_reward == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_separate_contexts(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "exploring", "go", True, 5.0)
        await db.update_action_stats("Anima", "near_player", "speak", True, 3.0)

        exploring = await db.get_action_stats("Anima", "exploring")
        near_player = await db.get_action_stats("Anima", "near_player")
        assert len(exploring) == 1
        assert len(near_player) == 1
        assert exploring[0].action == "go"
        assert near_player[0].action == "speak"

    @pytest.mark.asyncio
    async def test_get_all_stats(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "ctx1", "go", True, 5.0)
        await db.update_action_stats("Anima", "ctx2", "speak", True, 3.0)

        all_stats = await db.get_all_action_stats("Anima")
        assert len(all_stats) == 2
