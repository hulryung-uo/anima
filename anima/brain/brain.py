"""Brain: polls events and ticks the behavior tree."""

from __future__ import annotations

import time

import structlog

from anima.action.speech import respond_to_speech
from anima.brain.behavior_tree import (
    Action,
    BrainContext,
    Condition,
    Node,
    Selector,
    Sequence,
    Status,
)
from anima.brain.think import llm_think
from anima.perception.event_stream import GameEventType
from anima.skills.forum_action import forum_read_action, forum_write_action

logger = structlog.get_logger()

# Cooldown between skill executions (seconds)
SKILL_COOLDOWN = 10.0


def _has_low_hp(ctx: BrainContext) -> bool:
    return ctx.perception.self_state.hp_percent < 30.0


def _has_pending_speech(ctx: BrainContext) -> bool:
    return bool(ctx.blackboard.get("pending_speech"))


def _has_forum(ctx: BrainContext) -> bool:
    return ctx.blackboard.get("forum_client") is not None


def _has_skill_registry(ctx: BrainContext) -> bool:
    return ctx.blackboard.get("skill_registry") is not None


async def _flee_action(ctx: BrainContext) -> Status:
    """Placeholder flee action — just log for now."""
    logger.warning(
        "flee_triggered",
        hp=ctx.perception.self_state.hits,
        hp_max=ctx.perception.self_state.hits_max,
    )
    return Status.SUCCESS


async def _skill_action(ctx: BrainContext) -> Status:
    """Select and execute a skill using the Q-learning selector."""
    from anima.skills.base import SkillRegistry
    from anima.skills.selector import SkillSelector

    now = time.time()
    last_skill = ctx.blackboard.get("last_skill_time", 0.0)
    if now - last_skill < SKILL_COOLDOWN:
        return Status.FAILURE

    registry: SkillRegistry | None = ctx.blackboard.get("skill_registry")
    if registry is None or ctx.memory_db is None:
        return Status.FAILURE

    agent_name = _agent_name(ctx)
    available = await registry.available_skills(ctx)
    if not available:
        return Status.FAILURE

    selector = SkillSelector(ctx.memory_db)
    skill = await selector.select(ctx, available, agent_name)
    if skill is None:
        return Status.FAILURE

    logger.info("skill_executing", skill=skill.name, category=skill.category)
    ctx.blackboard["last_skill_time"] = now

    result = await skill.execute(ctx)

    # Update Q-values
    next_available = await registry.available_skills(ctx)
    await selector.update(ctx, skill, result, agent_name, next_available)

    # Record episode in memory
    if ctx.memory_db:
        await ctx.memory_db.record_episode(
            agent_name=agent_name,
            location_x=ctx.perception.self_state.x,
            location_y=ctx.perception.self_state.y,
            action=skill.name,
            target=result.message[:50],
            outcome="success" if result.success else "failure",
            reward=result.reward,
            summary=result.message,
        )

    return Status.SUCCESS if result.success else Status.FAILURE


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"


def build_default_tree() -> Node:
    """Build the default behavior tree.

    Selector
    +-- Sequence [Survival]  -- HP<30% -> flee
    +-- Sequence [Social]    -- speech heard -> respond
    +-- Sequence [Forum]     -- forum enabled -> read/write posts
    +-- Sequence [SkillExec] -- skill registry -> Q-select + execute
    +-- Action [Think]       -- LLM decides: move, speak, explore
    """
    return Selector(
        "root",
        [
            Sequence(
                "survival",
                [
                    Condition("low_hp", _has_low_hp),
                    Action("flee", _flee_action),
                ],
            ),
            Sequence(
                "social",
                [
                    Condition("speech_pending", _has_pending_speech),
                    Action("respond", respond_to_speech),
                ],
            ),
            Sequence(
                "forum",
                [
                    Condition("has_forum", _has_forum),
                    Selector(
                        "forum_ops",
                        [
                            Action("forum_write", forum_write_action),
                            Action("forum_read", forum_read_action),
                        ],
                    ),
                ],
            ),
            Sequence(
                "skill_exec",
                [
                    Condition("has_skills", _has_skill_registry),
                    Action("skill_select", _skill_action),
                ],
            ),
            Action("think", llm_think),
        ],
    )


class Brain:
    """Top-level brain: polls events, updates blackboard, ticks BT."""

    def __init__(self, context: BrainContext, root: Node | None = None) -> None:
        self.context = context
        self.root = root or build_default_tree()

    async def tick(self) -> Status:
        """One brain tick: poll events into blackboard, then run the tree."""
        self._poll_events()
        return await self.root.tick(self.context)

    def _poll_events(self) -> None:
        events = self.context.perception.poll_events()
        my_serial = self.context.perception.self_state.serial
        for event in events:
            if event.type == GameEventType.SPEECH_HEARD:
                serial = event.data.get("serial", 0)
                # Skip own speech and system messages
                if serial == my_serial or serial == 0xFFFFFFFF:
                    continue
                if event.data.get("name", "").lower() == "system":
                    continue
                pending = self.context.blackboard.setdefault("pending_speech", [])
                pending.append(event.data)
                # Track last time someone spoke to us
                self.context.blackboard["last_player_speech"] = time.time()
