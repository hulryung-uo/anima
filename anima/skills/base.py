"""Skill system base classes — Skill ABC, SkillResult, SkillRegistry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext


@dataclass
class SkillResult:
    """Result of executing a skill."""

    success: bool
    reward: float
    message: str = ""
    items_gained: list[int] = field(default_factory=list)
    items_consumed: list[int] = field(default_factory=list)
    skill_gains: list[tuple[int, float]] = field(default_factory=list)
    duration_ms: float = 0.0


class Skill(ABC):
    """Abstract base class for all game skills.

    Each skill is a self-contained action unit that:
    - Checks preconditions (can_execute)
    - Executes a packet sequence (execute)
    - Returns a structured result with reward signal
    """

    name: str = ""
    category: str = ""
    description: str = ""

    # Preconditions — subclasses set these as class attributes
    required_items: list[int] = []       # item graphics needed in backpack
    required_nearby: list[int] = []      # object graphics needed nearby
    required_skill: tuple[int, float] | None = None  # (skill_id, min_value)
    required_stats: dict[str, int] = {}  # e.g. {"str": 30}

    async def can_execute(self, ctx: BrainContext) -> bool:
        """Check if all preconditions are met."""
        ss = ctx.perception.self_state
        world = ctx.perception.world

        # Check required items in backpack
        if self.required_items:
            backpack = ss.equipment.get(0x15)  # Layer.BACKPACK
            if not backpack:
                return False
            backpack_items = [
                it for it in world.items.values()
                if it.container == backpack
            ]
            backpack_graphics = {it.graphic for it in backpack_items}
            for graphic in self.required_items:
                if graphic not in backpack_graphics:
                    return False

        # Check required nearby objects
        if self.required_nearby:
            nearby_items = world.nearby_items(ss.x, ss.y, distance=3)
            nearby_graphics = {it.graphic for it in nearby_items}
            nearby_mobs = world.nearby_mobiles(ss.x, ss.y, distance=3)
            nearby_bodies = {m.body for m in nearby_mobs}
            all_nearby = nearby_graphics | nearby_bodies
            if not any(g in all_nearby for g in self.required_nearby):
                return False

        # Check required UO skill level
        if self.required_skill is not None:
            skill_id, min_val = self.required_skill
            skill_info = ss.skills.get(skill_id)
            if skill_info is None or skill_info.value < min_val:
                return False

        # Check required stats
        for stat_name, min_val in self.required_stats.items():
            actual = getattr(ss, stat_name, 0)
            if actual < min_val:
                return False

        return True

    @abstractmethod
    async def execute(self, ctx: BrainContext) -> SkillResult:
        """Execute the skill. Returns result with reward signal."""
        ...

    def __repr__(self) -> str:
        return f"<Skill {self.name}>"


class SkillRegistry:
    """Registry of all available skills."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    @property
    def all_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def by_category(self, category: str) -> list[Skill]:
        return [s for s in self._skills.values() if s.category == category]

    async def available_skills(self, ctx: BrainContext) -> list[Skill]:
        """Return skills whose preconditions are currently met."""
        result = []
        for skill in self._skills.values():
            if await skill.can_execute(ctx):
                result.append(skill)
        return result

    def describe_all(self) -> str:
        """Build a description of all skills for LLM context."""
        lines = []
        for skill in self._skills.values():
            lines.append(f"- {skill.name} [{skill.category}]: {skill.description}")
        return "\n".join(lines)
