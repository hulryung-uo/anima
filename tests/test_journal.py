"""Tests for the activity journal system."""

import pytest

from anima.memory.database import MemoryDB
from anima.memory.journal import (
    ActivityJournal,
    build_narrative,
    result_to_importance,
    result_to_mood,
)
from anima.skills.base import SkillResult


@pytest.fixture
async def journal():
    db = MemoryDB(":memory:")
    await db.init()
    j = ActivityJournal(db, agent_name="TestAgent")
    yield j
    await db.close()


class TestBuildNarrative:
    def test_known_skill_success(self):
        result = SkillResult(success=True, reward=5.0, message="Got logs")
        text = build_narrative("Tormund", "chop_wood", result)
        assert "Tormund" in text
        assert "벌목" in text

    def test_known_skill_failure(self):
        result = SkillResult(success=False, reward=-1.0, message="No trees")
        text = build_narrative("Tormund", "chop_wood", result)
        assert "실패" in text

    def test_craft_tinker_with_detail(self):
        result = SkillResult(success=True, reward=8.0, message="Crafted Hatchet")
        text = build_narrative("Tormund", "craft_tinker", result)
        assert "팅커링" in text
        assert "Crafted Hatchet" in text

    def test_unknown_skill_fallback(self):
        result = SkillResult(success=True, reward=3.0, message="Did something")
        text = build_narrative("Tormund", "unknown_skill", result)
        assert "Tormund" in text
        assert "unknown_skill" in text

    def test_sell_success(self):
        result = SkillResult(success=True, reward=5.0, message="Sold 3 items for 150gp")
        text = build_narrative("Tormund", "sell_to_npc", result)
        assert "팔았다" in text


class TestMoodAndImportance:
    def test_excited_mood(self):
        result = SkillResult(success=True, reward=8.0, message="")
        assert result_to_mood(result) == "excited"

    def test_satisfied_mood(self):
        result = SkillResult(success=True, reward=3.0, message="")
        assert result_to_mood(result) == "satisfied"

    def test_frustrated_mood(self):
        result = SkillResult(success=False, reward=-5.0, message="")
        assert result_to_mood(result) == "frustrated"

    def test_neutral_mood(self):
        result = SkillResult(success=False, reward=-1.0, message="")
        assert result_to_mood(result) == "neutral"

    def test_significant_importance(self):
        result = SkillResult(success=True, reward=10.0, message="")
        assert result_to_importance(result) == 3

    def test_notable_importance(self):
        result = SkillResult(success=True, reward=4.0, message="")
        assert result_to_importance(result) == 2

    def test_routine_importance(self):
        result = SkillResult(success=False, reward=-1.0, message="")
        assert result_to_importance(result) == 1


class TestActivityJournal:
    @pytest.mark.asyncio
    async def test_record_skill(self, journal):
        result = SkillResult(success=True, reward=5.0, message="Got logs")
        entry = await journal.record_skill("chop_wood", result, x=100, y=200)
        assert entry.id > 0
        assert entry.category == "gathering"
        assert "벌목" in entry.narrative
        assert entry.mood == "excited"
        assert entry.location_x == 100

    @pytest.mark.asyncio
    async def test_record_event(self, journal):
        entry = await journal.record_event(
            "새로운 도시를 발견했다!",
            category="exploration",
            x=1495,
            y=1629,
            mood="excited",
            importance=3,
        )
        assert entry.id > 0
        assert entry.narrative == "새로운 도시를 발견했다!"

    @pytest.mark.asyncio
    async def test_recent_entries(self, journal):
        r1 = SkillResult(success=True, reward=5.0, message="logs")
        r2 = SkillResult(success=True, reward=8.0, message="Hatchet")
        await journal.record_skill("chop_wood", r1)
        await journal.record_skill("craft_tinker", r2)

        entries = await journal.recent_entries(limit=10)
        assert len(entries) == 2
        # Chronological order
        assert entries[0].action == "chop_wood"
        assert entries[1].action == "craft_tinker"

    @pytest.mark.asyncio
    async def test_recent_entries_filter_category(self, journal):
        r1 = SkillResult(success=True, reward=5.0, message="logs")
        r2 = SkillResult(success=True, reward=8.0, message="Hatchet")
        await journal.record_skill("chop_wood", r1)
        await journal.record_skill("craft_tinker", r2)

        entries = await journal.recent_entries(category="crafting")
        assert len(entries) == 1
        assert entries[0].action == "craft_tinker"

    @pytest.mark.asyncio
    async def test_recent_entries_filter_importance(self, journal):
        r1 = SkillResult(success=False, reward=-1.0, message="fail")  # importance=1
        r2 = SkillResult(success=True, reward=10.0, message="big win")  # importance=3
        await journal.record_skill("chop_wood", r1)
        await journal.record_skill("craft_tinker", r2)

        entries = await journal.recent_entries(min_importance=2)
        assert len(entries) == 1
        assert entries[0].action == "craft_tinker"

    @pytest.mark.asyncio
    async def test_compile_narrative(self, journal):
        r1 = SkillResult(success=True, reward=5.0, message="logs")
        r2 = SkillResult(success=True, reward=8.0, message="Hatchet")
        r3 = SkillResult(success=True, reward=3.0, message="Sold items")
        await journal.record_skill("chop_wood", r1)
        await journal.record_skill("craft_tinker", r2)
        await journal.record_skill("sell_to_npc", r3)

        narrative = await journal.compile_narrative(hours=1.0)
        assert len(narrative) > 0
        # Should have multiple paragraphs (gathering, crafting, trade)
        assert "\n\n" in narrative

    @pytest.mark.asyncio
    async def test_summarize_day(self, journal):
        r = SkillResult(success=True, reward=5.0, message="ok")
        await journal.record_skill("chop_wood", r)
        await journal.record_skill("chop_wood", r)
        await journal.record_skill("craft_tinker", r)

        summary = await journal.summarize_day()
        assert summary["gathering"] == 2
        assert summary["crafting"] == 1

    @pytest.mark.asyncio
    async def test_prune(self, journal):
        r = SkillResult(success=True, reward=1.0, message="ok")
        for _ in range(10):
            await journal.record_skill("chop_wood", r)

        deleted = await journal.prune(max_entries=5)
        assert deleted == 5

        entries = await journal.recent_entries(limit=100)
        assert len(entries) == 5
