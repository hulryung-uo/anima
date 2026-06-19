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
        title, text = build_narrative("Tormund", "chop_wood", result)
        assert "Tormund" in text
        assert "벌목" in text
        assert "벌목" in title
        assert "Got logs" in text  # detail included

    def test_known_skill_failure(self):
        result = SkillResult(success=False, reward=-1.0, message="No trees")
        title, text = build_narrative("Tormund", "chop_wood", result)
        assert "실패" in text
        assert "실패" in title

    def test_craft_tinker_with_detail(self):
        result = SkillResult(success=True, reward=8.0, message="Crafted Hatchet")
        title, text = build_narrative("Tormund", "craft_tinker", result)
        assert "팅커링" in text
        assert "Crafted Hatchet" in text
        # Notable event (reward >= 3.0) includes detail in title
        assert "Crafted Hatchet" in title

    def test_unknown_skill_fallback(self):
        result = SkillResult(success=True, reward=3.0, message="Did something")
        title, text = build_narrative("Tormund", "unknown_skill", result)
        assert "Tormund" in text
        assert "unknown_skill" in text
        assert "성공" in title

    def test_sell_success(self):
        result = SkillResult(success=True, reward=5.0, message="Sold 3 items for 150gp")
        title, text = build_narrative("Tormund", "sell_to_npc", result)
        assert "팔았다" in text
        assert "판매" in title


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

    def test_catastrophic_negative_is_significant(self):
        # A large negative reward (death / heavy damage) is as memorable as a
        # windfall and must not be filed as routine and then filtered out of
        # min_importance>=2 memory retrieval.
        result = SkillResult(success=False, reward=-10.0, message="Died")
        assert result_to_importance(result) == 3

    def test_painful_negative_is_notable(self):
        result = SkillResult(success=False, reward=-5.0, message="Hurt badly")
        assert result_to_importance(result) == 2

    def test_mild_negative_stays_routine(self):
        result = SkillResult(success=False, reward=-2.0, message="Minor miss")
        assert result_to_importance(result) == 1


class TestActivityJournal:
    @pytest.mark.asyncio
    async def test_record_skill(self, journal):
        result = SkillResult(success=True, reward=5.0, message="Got logs")
        entry = await journal.record_skill("chop_wood", result, x=100, y=200)
        assert entry.id > 0
        assert entry.category == "gathering"
        assert "벌목" in entry.narrative
        assert entry.title  # title should not be empty
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
        # Should have multiple sections with category headers
        assert "\n\n" in narrative
        assert "###" in narrative  # section headers present

    @pytest.mark.asyncio
    async def test_compile_narrative_groups_interleaved_categories(self, journal):
        # Entries are stored/returned in timestamp order, so the same category
        # can recur interleaved with others (gather → craft → gather). The
        # compiled essay must collect each category under ONE heading, not emit
        # a fresh "자원 채집" header for every chronological run — otherwise the
        # forum essay / self-reflection reads the same activity scattered across
        # duplicate sections.
        gather = SkillResult(success=True, reward=5.0, message="logs")
        craft = SkillResult(success=True, reward=8.0, message="Hatchet")
        await journal.record_skill("chop_wood", gather)  # gathering
        await journal.record_skill("craft_tinker", craft)  # crafting
        await journal.record_skill("mine_ore", gather)  # gathering again

        narrative = await journal.compile_narrative(hours=1.0)

        # The gathering header must appear exactly once despite the interleave...
        assert narrative.count("### 자원 채집") == 1
        assert narrative.count("### 제작 활동") == 1
        # ...and BOTH gathering narratives live under that single header.
        gather_section = narrative.split("### 자원 채집", 1)[1].split("###", 1)[0]
        assert "통나무를 얻었다" in gather_section  # chop_wood success line
        assert "광석을 캐내는 데 성공했다" in gather_section  # mine_ore success line

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

    @pytest.mark.asyncio
    async def test_prune_preserves_significant_memories(self, journal):
        # Record a handful of OLD but significant events (importance=3), then a
        # flood of NEWER routine noise (importance=1). Pruning past the cap must
        # drop the routine noise, not the meaningful memories — even though the
        # significant events are the oldest rows by timestamp.
        big = SkillResult(success=True, reward=10.0, message="windfall")  # importance=3
        routine = SkillResult(success=False, reward=-1.0, message="miss")  # importance=1
        assert result_to_importance(big) == 3
        assert result_to_importance(routine) == 1

        significant_titles = []
        for i in range(3):
            entry = await journal.record_event(
                f"기념비적 사건 {i}",
                category="event",
                title=f"milestone-{i}",
                mood="excited",
                importance=3,
            )
            significant_titles.append(entry.title)
        # A flood of newer, routine entries.
        for _ in range(20):
            await journal.record_skill("chop_wood", routine)

        # 23 entries -> keep only 5.
        deleted = await journal.prune(max_entries=5)
        assert deleted == 18

        survivors = await journal.recent_entries(limit=100, min_importance=1)
        assert len(survivors) == 5
        kept_titles = {e.title for e in survivors}
        # All three significant memories must survive the prune.
        for t in significant_titles:
            assert t in kept_titles
        # And at least one survived purely on importance, not recency, proving
        # the prune isn't just keeping the newest rows.
        assert any(e.importance == 3 for e in survivors)
