"""Skill selector — Q-table with UCB1 exploration for skill selection."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

import structlog

from anima.memory.database import MemoryDB
from anima.skills.base import Skill, SkillResult
from anima.skills.state import encode_state, region_coords

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
        """Select the best skill to execute using UCB1.

        Returns None if no skills are available.
        """
        if not available:
            return None

        if len(available) == 1:
            return available[0]

        state_key = encode_state(ctx)
        q_values = await self._db.get_q_values(agent_name, state_key)

        total_visits = sum(v[1] for v in q_values.values()) if q_values else 0

        best_score = -math.inf
        best_skill = available[0]

        for skill in available:
            q, visits = q_values.get(skill.name, (0.0, 0))

            if visits == 0:
                # Never tried — always explore first
                score = float("inf")
            else:
                # UCB1: exploitation + exploration bonus
                exploration = UCB_C * math.sqrt(
                    math.log(max(total_visits, 1)) / visits
                )
                score = q + exploration

            if score > best_score:
                best_score = score
                best_skill = skill

        # Break ties randomly among infinite scores (untried skills)
        untried = [
            s for s in available
            if q_values.get(s.name, (0.0, 0))[1] == 0
        ]
        if untried:
            best_skill = random.choice(untried)

        logger.debug(
            "skill_selected",
            skill=best_skill.name,
            state=state_key,
            score=f"{best_score:.2f}" if best_score != float("inf") else "inf",
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
        """Update Q-value after executing a skill."""
        state_key = encode_state(ctx)

        # Current Q-value
        old_q = await self._db.get_q_value(agent_name, state_key, skill.name)
        q_values = await self._db.get_q_values(agent_name, state_key)
        old_visits = q_values.get(skill.name, (0.0, 0))[1]

        # Max Q-value for next state (for Bellman equation)
        # Use current state as approximation since we don't have the true next state yet
        max_next_q = 0.0
        if next_available:
            next_state = encode_state(ctx)
            next_q = await self._db.get_q_values(agent_name, next_state)
            if next_q:
                max_next_q = max(v[0] for v in next_q.values())

        # Q-learning update: Q(s,a) += α * (r + γ*max(Q(s')) - Q(s,a))
        new_q = old_q + ALPHA * (result.reward + GAMMA * max_next_q - old_q)

        await self._db.update_q_value(
            agent_name, state_key, skill.name,
            q_value=new_q,
            visit_count=old_visits + 1,
        )

        # Update location value map
        ss = ctx.perception.self_state
        rx, ry = region_coords(ss.x, ss.y)
        await self._db.update_location_value(
            agent_name, rx, ry, skill.name, result.reward,
        )

        logger.info(
            "skill_q_updated",
            skill=skill.name,
            state=state_key,
            reward=f"{result.reward:+.1f}",
            q=f"{old_q:.2f}→{new_q:.2f}",
            visits=old_visits + 1,
        )
