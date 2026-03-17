"""Tests for the learning/reflection system."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.memory.database import MemoryDB
from anima.memory.learning import reflect
from anima.memory.rewards import REWARDS, get_reward  # noqa: F401

# ---------------------------------------------------------------------------
# Rewards tests
# ---------------------------------------------------------------------------


class TestRewards:
    def test_known_reward(self) -> None:
        assert get_reward("goal_arrived") == 10.0
        assert get_reward("damage_taken") == -10.0

    def test_unknown_reward(self) -> None:
        assert get_reward("unknown_signal") == 0.0

    def test_all_rewards_are_float(self) -> None:
        for key, val in REWARDS.items():
            assert isinstance(val, float), f"{key} reward should be float"


# ---------------------------------------------------------------------------
# Reflection tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


class TestReflection:
    @pytest.mark.asyncio
    async def test_reflect_extracts_facts(self, db: MemoryDB) -> None:
        # Record enough episodes for reflection
        for i in range(10):
            await db.record_episode(
                "Anima", 1000, 2000, "go", f"place_{i}", "success", reward=5.0,
                summary=f"Visited place_{i}",
            )

        # Mock LLM
        mock_llm = MagicMock()
        llm_text = (
            "The area around (1000, 2000) has many interesting places\n"
            "Visiting new places is consistently rewarding"
        )
        mock_llm.chat = AsyncMock(
            return_value=MagicMock(text=llm_text)
        )

        facts = await reflect(db, mock_llm, "Anima")
        assert len(facts) == 2

        # Facts should be in knowledge table
        knowledge = await db.query_knowledge("Anima")
        assert len(knowledge) == 2
        assert all(k.source == "reflection" for k in knowledge)

    @pytest.mark.asyncio
    async def test_reflect_skips_few_episodes(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")

        mock_llm = MagicMock()
        facts = await reflect(db, mock_llm, "Anima")
        assert facts == []
        # LLM should not be called
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflect_handles_empty_llm(self, db: MemoryDB) -> None:
        for i in range(6):
            await db.record_episode("Anima", 1000, 2000, "go", f"p{i}", "success")

        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=MagicMock(text=""))

        facts = await reflect(db, mock_llm, "Anima")
        assert facts == []
