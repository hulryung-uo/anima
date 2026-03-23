"""Skill selector — Q-table with UCB1 exploration for skill selection."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog

from anima.memory.database import MemoryDB
from anima.skills.base import Skill, SkillResult
from anima.skills.state import encode_state

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Q-learning hyperparameters
ALPHA = 0.1    # learning rate
GAMMA = 0.9    # discount factor
UCB_C = 1.41   # exploration constant (sqrt(2) ≈ 1.41 is standard)


class SkillSelector:
    """Selects which skill to execute using Q-learning + UCB1 exploration."""

    def __init__(self, memory_db: MemoryDB) -> None:
        self._db = memory_db

    async def select(
        self,
        ctx: BrainContext,
        available: list[Skill],
        agent_name: str,
    ) -> Skill | None:
        """Select a skill randomly (Q-learning disabled).

        Returns None if no skills are available.
        """
        if not available:
            return None

        best_skill = random.choice(available)

        logger.debug(
            "skill_selected",
            skill=best_skill.name,
            mode="random",
            available=[s.name for s in available],
        )
        return best_skill

    async def update(
        self,
        ctx: BrainContext,
        skill: Skill,
        result: SkillResult,
        agent_name: str,
        next_available: list[Skill] | None = None,
    ) -> None:
        """Update Q-value after executing a skill. (DISABLED)"""
        logger.debug(
            "skill_q_update_skipped",
            skill=skill.name,
            reward=f"{result.reward:+.1f}",
            reason="q-learning disabled",
        )
