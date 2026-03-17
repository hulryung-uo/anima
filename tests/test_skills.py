"""Tests for the skill system infrastructure."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from anima.memory.database import MemoryDB
from anima.skills.base import Skill, SkillRegistry, SkillResult
from anima.skills.selector import SkillSelector
from anima.skills.state import encode_state, region_coords

# ---------------------------------------------------------------------------
# Test skill implementations
# ---------------------------------------------------------------------------


class DummySkill(Skill):
    name = "dummy"
    category = "test"
    description = "A test skill"

    async def execute(self, ctx):
        return SkillResult(success=True, reward=5.0, message="ok")


class FailSkill(Skill):
    name = "fail_skill"
    category = "test"
    description = "Always fails"

    async def execute(self, ctx):
        return SkillResult(success=False, reward=-2.0, message="failed")


class NeedsItemSkill(Skill):
    name = "needs_item"
    category = "test"
    description = "Needs a specific item"
    required_items = [0x0E86]  # pickaxe

    async def execute(self, ctx):
        return SkillResult(success=True, reward=3.0, message="ok")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(**overrides):
    """Create a minimal BrainContext for testing."""
    from anima.brain.behavior_tree import BrainContext

    perception = MagicMock()
    perception.self_state.x = 1000
    perception.self_state.y = 2000
    perception.self_state.hp_percent = 100.0
    perception.self_state.hits = 100
    perception.self_state.hits_max = 100
    perception.self_state.strength = 50
    perception.self_state.dexterity = 30
    perception.self_state.intelligence = 20
    perception.self_state.weight = 50
    perception.self_state.weight_max = 400
    perception.self_state.equipment = {}
    perception.self_state.skills = {}
    perception.world.nearby_items.return_value = []
    perception.world.nearby_mobiles.return_value = []
    perception.world.items = {}

    defaults = {
        "perception": perception,
        "conn": MagicMock(),
        "walker": MagicMock(),
        "map_reader": None,
        "cfg": MagicMock(),
        "memory_db": None,
    }
    defaults.update(overrides)
    return BrainContext(**defaults)


# ---------------------------------------------------------------------------
# SkillResult tests
# ---------------------------------------------------------------------------


class TestSkillResult:
    def test_defaults(self) -> None:
        r = SkillResult(success=True, reward=5.0)
        assert r.success is True
        assert r.reward == 5.0
        assert r.message == ""
        assert r.items_gained == []

    def test_with_details(self) -> None:
        r = SkillResult(
            success=True, reward=10.0, message="Mined 3 iron ore",
            items_gained=[123, 456], skill_gains=[(45, 0.1)],
        )
        assert len(r.items_gained) == 2
        assert r.skill_gains[0] == (45, 0.1)


# ---------------------------------------------------------------------------
# Skill ABC tests
# ---------------------------------------------------------------------------


class TestSkill:
    @pytest.mark.asyncio
    async def test_can_execute_no_requirements(self) -> None:
        skill = DummySkill()
        ctx = make_ctx()
        assert await skill.can_execute(ctx) is True

    @pytest.mark.asyncio
    async def test_can_execute_missing_item(self) -> None:
        skill = NeedsItemSkill()
        ctx = make_ctx()
        # No backpack
        assert await skill.can_execute(ctx) is False

    @pytest.mark.asyncio
    async def test_execute(self) -> None:
        skill = DummySkill()
        ctx = make_ctx()
        result = await skill.execute(ctx)
        assert result.success is True
        assert result.reward == 5.0

    def test_repr(self) -> None:
        skill = DummySkill()
        assert "dummy" in repr(skill)


# ---------------------------------------------------------------------------
# SkillRegistry tests
# ---------------------------------------------------------------------------


class TestSkillRegistry:
    def test_register_and_get(self) -> None:
        reg = SkillRegistry()
        skill = DummySkill()
        reg.register(skill)
        assert reg.get("dummy") is skill
        assert reg.get("nonexistent") is None

    def test_all_skills(self) -> None:
        reg = SkillRegistry()
        reg.register(DummySkill())
        reg.register(FailSkill())
        assert len(reg.all_skills) == 2

    def test_by_category(self) -> None:
        reg = SkillRegistry()
        reg.register(DummySkill())
        reg.register(FailSkill())
        test_skills = reg.by_category("test")
        assert len(test_skills) == 2
        assert len(reg.by_category("combat")) == 0

    @pytest.mark.asyncio
    async def test_available_skills(self) -> None:
        reg = SkillRegistry()
        reg.register(DummySkill())
        reg.register(NeedsItemSkill())
        ctx = make_ctx()
        available = await reg.available_skills(ctx)
        # DummySkill has no requirements, NeedsItemSkill needs a pickaxe
        assert len(available) == 1
        assert available[0].name == "dummy"

    def test_describe_all(self) -> None:
        reg = SkillRegistry()
        reg.register(DummySkill())
        desc = reg.describe_all()
        assert "dummy" in desc
        assert "test" in desc


# ---------------------------------------------------------------------------
# State encoder tests
# ---------------------------------------------------------------------------


class TestStateEncoder:
    def test_encode_basic(self) -> None:
        ctx = make_ctx()
        state = encode_state(ctx)
        assert isinstance(state, str)
        assert "|" in state
        parts = state.split("|")
        assert len(parts) == 5

    def test_hp_levels(self) -> None:
        ctx = make_ctx()
        ctx.perception.self_state.hp_percent = 100.0
        assert "full" in encode_state(ctx)

        ctx.perception.self_state.hp_percent = 60.0
        assert "healthy" in encode_state(ctx)

        ctx.perception.self_state.hp_percent = 30.0
        assert "wounded" in encode_state(ctx)

        ctx.perception.self_state.hp_percent = 10.0
        assert "critical" in encode_state(ctx)

    def test_region_coords(self) -> None:
        assert region_coords(1000, 2000) == (31, 62)
        assert region_coords(0, 0) == (0, 0)
        assert region_coords(31, 63) == (0, 1)
        assert region_coords(32, 64) == (1, 2)


# ---------------------------------------------------------------------------
# SkillSelector tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


class TestSkillSelector:
    @pytest.mark.asyncio
    async def test_select_single_skill(self, db: MemoryDB) -> None:
        selector = SkillSelector(db)
        ctx = make_ctx()
        skills = [DummySkill()]
        result = await selector.select(ctx, skills, "Anima")
        assert result is not None
        assert result.name == "dummy"

    @pytest.mark.asyncio
    async def test_select_empty(self, db: MemoryDB) -> None:
        selector = SkillSelector(db)
        ctx = make_ctx()
        result = await selector.select(ctx, [], "Anima")
        assert result is None

    @pytest.mark.asyncio
    async def test_selects_untried_first(self, db: MemoryDB) -> None:
        selector = SkillSelector(db)
        ctx = make_ctx()

        # Give dummy a Q-value, leave fail_skill untried
        state_key = encode_state(ctx)
        await db.update_q_value("Anima", state_key, "dummy", 5.0, 10)

        skills = [DummySkill(), FailSkill()]
        result = await selector.select(ctx, skills, "Anima")
        # Should pick the untried skill (fail_skill)
        assert result is not None
        assert result.name == "fail_skill"

    @pytest.mark.asyncio
    async def test_update_q_value(self, db: MemoryDB) -> None:
        selector = SkillSelector(db)
        ctx = make_ctx()

        skill = DummySkill()
        result = SkillResult(success=True, reward=10.0)
        await selector.update(ctx, skill, result, "Anima")

        state_key = encode_state(ctx)
        q = await db.get_q_value("Anima", state_key, "dummy")
        assert q > 0.0

    @pytest.mark.asyncio
    async def test_update_location_value(self, db: MemoryDB) -> None:
        selector = SkillSelector(db)
        ctx = make_ctx()

        skill = DummySkill()
        result = SkillResult(success=True, reward=10.0)
        await selector.update(ctx, skill, result, "Anima")

        rx, ry = region_coords(1000, 2000)
        locs = await db.get_location_values("Anima", rx, ry)
        assert len(locs) == 1
        assert locs[0][0] == "dummy"  # activity
        assert locs[0][1] == 10.0     # total_reward

    @pytest.mark.asyncio
    async def test_exploitation_over_time(self, db: MemoryDB) -> None:
        """After many updates, high-reward skill should be preferred."""
        selector = SkillSelector(db)
        ctx = make_ctx()
        state_key = encode_state(ctx)

        # Give dummy high Q, fail_skill low Q, both tried many times
        await db.update_q_value("Anima", state_key, "dummy", 8.0, 50)
        await db.update_q_value("Anima", state_key, "fail_skill", -2.0, 50)

        skills = [DummySkill(), FailSkill()]
        # With many visits, exploitation dominates — should pick dummy
        result = await selector.select(ctx, skills, "Anima")
        assert result is not None
        assert result.name == "dummy"
