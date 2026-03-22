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

# forum_read/write are now registered as skills, not BT nodes
from anima.skills.state import encode_state

logger = structlog.get_logger()

# Cooldown between skill executions (seconds)
SKILL_COOLDOWN = 0.5  # minimal gap between skill executions


def _has_low_hp(ctx: BrainContext) -> bool:
    return ctx.perception.self_state.hp_percent < 30.0


def _has_pending_speech(ctx: BrainContext) -> bool:
    return bool(ctx.blackboard.get("pending_speech"))


def _has_skill_registry(ctx: BrainContext) -> bool:
    return ctx.blackboard.get("skill_registry") is not None


async def _flee_action(ctx: BrainContext) -> Status:
    """Placeholder flee action — just log for now."""
    logger.warning(
        "flee_triggered",
        hp=ctx.perception.self_state.hits,
        hp_max=ctx.perception.self_state.hits_max,
    )
    from anima.core.publish import pub
    ss = ctx.perception.self_state
    pub(ctx, "combat.flee", f"Flee! HP={ss.hits}/{ss.hits_max}", importance=3)
    return Status.SUCCESS


async def _skill_action(ctx: BrainContext) -> Status:
    """Select and execute a skill using the Q-learning selector."""
    from anima.skills.base import SkillRegistry
    from anima.skills.selector import SkillSelector

    # Prevent concurrent execution — skill may be awaiting server responses
    if ctx.blackboard.get("skill_running"):
        return Status.FAILURE

    # Don't execute skills while actively walking to a destination
    move_target = ctx.blackboard.get("move_target")
    if move_target is not None:
        sx = ctx.perception.self_state.x
        sy = ctx.perception.self_state.y
        tx, ty = move_target
        # Only run skills if we've arrived (within 2 tiles)
        if abs(sx - tx) > 2 or abs(sy - ty) > 2:
            return Status.FAILURE

    now = time.time()
    last_skill = ctx.blackboard.get("last_skill_time", 0.0)
    if now - last_skill < SKILL_COOLDOWN:
        return Status.FAILURE

    registry: SkillRegistry | None = ctx.blackboard.get("skill_registry")
    if registry is None or ctx.memory_db is None:
        return Status.FAILURE

    # If a skill has failed too many times in a row, skip skill_exec
    # and let Think/LLM figure out what to do
    consecutive_fails = ctx.blackboard.get("skill_consecutive_fails", 0)
    if consecutive_fails >= 5:
        ctx.blackboard["skill_consecutive_fails"] = 0
        # Schedule a rethink soon but not instantly — halve the cooldown
        ctx.blackboard["last_think_time"] = time.time() - 15.0
        ctx.blackboard["skill_problem"] = (
            f"Last skill failed {consecutive_fails} times in a row. "
            f"May need to move elsewhere, get materials, or try something different."
        )
        logger.info("skill_too_many_fails", fails=consecutive_fails)
        from anima.core.publish import pub
        pub(ctx, "brain.rethink", "Too many skill failures, rethinking...", importance=2)

        # Generate problem report after 10+ failures
        if consecutive_fails >= 10:
            from anima.monitor.report import report_problem
            await report_problem(
                ctx,
                problem=f"Skill execution failed {consecutive_fails} times consecutively",
                expected="Skills should succeed occasionally with proper materials and location",
                actual="All attempts failed — may be missing materials, wrong location, or stuck",
            )

        return Status.FAILURE

    agent_name = _agent_name(ctx)
    available = await registry.available_skills(ctx)
    if not available:
        # Build detailed diagnostic so LLM can take corrective action
        reasons: list[str] = []
        for s in registry.all_skills:
            reason = await s.diagnose(ctx)
            if reason:
                reasons.append(f"{s.name}: {reason}")
        detail = "; ".join(reasons) if reasons else "unknown"
        ctx.blackboard["skill_problem"] = (
            f"No skills can execute right now. {detail}. "
            "Consider going to a shop to buy tools, or moving to a new area."
        )
        return Status.FAILURE

    logger.debug(
        "skills_available",
        skills=[s.name for s in available],
        count=len(available),
    )

    selector = SkillSelector(ctx.memory_db)
    skill = await selector.select(ctx, available, agent_name)
    if skill is None:
        return Status.FAILURE

    logger.info("skill_executing", skill=skill.name, category=skill.category)
    ctx.blackboard["skill_running"] = True

    from anima.core.publish import pub
    pub(ctx, "action.start", f"Executing {skill.name}", skill=skill.name)

    try:
        result = await skill.execute(ctx)
    finally:
        ctx.blackboard["skill_running"] = False
        ctx.blackboard["last_skill_time"] = time.time()

    # Track consecutive failures (total + per-skill)
    if result.success:
        ctx.blackboard["skill_consecutive_fails"] = 0
        ctx.blackboard["last_failed_skill"] = None
        ctx.blackboard["same_skill_fails"] = 0
    else:
        ctx.blackboard["skill_consecutive_fails"] = (
            ctx.blackboard.get("skill_consecutive_fails", 0) + 1
        )
        # Track same-skill repeated failures
        last_failed = ctx.blackboard.get("last_failed_skill")
        if last_failed == skill.name:
            same = ctx.blackboard.get("same_skill_fails", 0) + 1
            ctx.blackboard["same_skill_fails"] = same
            if same >= 3:
                # Same skill failing 3+ times → force rethink
                ctx.blackboard["same_skill_fails"] = 0
                ctx.blackboard["last_think_time"] = time.time() - 15.0
                ctx.blackboard.setdefault("skill_problem", (
                    f"{skill.name} failed {same} times in a row: "
                    f"{result.message}"
                ))
                logger.info(
                    "same_skill_loop",
                    skill=skill.name, fails=same,
                    msg=result.message[:60],
                )
        else:
            ctx.blackboard["last_failed_skill"] = skill.name
            ctx.blackboard["same_skill_fails"] = 1

    # Record to metrics
    mc = ctx.blackboard.get("metrics")
    if mc:
        event = f"{skill.category}_success" if result.success else f"{skill.category}_fail"
        mc.record(event, {"skill": skill.name, "reward": result.reward})

    icon = "OK" if result.success else "FAIL"
    pub(
        ctx, "action.end",
        f"{icon}: {skill.name} (reward={result.reward:+.1f}) {result.message[:60]}",
        importance=2 if result.success else 1,
        skill=skill.name, reward=result.reward, success=result.success,
    )

    # Update Q-values
    next_available = await registry.available_skills(ctx)
    await selector.update(ctx, skill, result, agent_name, next_available)

    # Snapshot Q-values for TUI
    q_values = await selector._db.get_q_values(agent_name, encode_state(ctx))
    ctx.blackboard["q_snapshot"] = q_values

    # Record episode in memory
    if ctx.memory_db:
        ss = ctx.perception.self_state
        await ctx.memory_db.record_episode(
            agent_name=agent_name,
            location_x=ss.x,
            location_y=ss.y,
            action=skill.name,
            target=result.message[:50],
            outcome="success" if result.success else "failure",
            reward=result.reward,
            summary=result.message,
        )

        # Record narrative journal entry
        journal = ctx.blackboard.get("journal")
        if journal is not None:
            await journal.record_skill(skill.name, result, x=ss.x, y=ss.y)

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
