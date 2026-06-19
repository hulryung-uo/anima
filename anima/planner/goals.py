"""Goal stack — gives the planner a sense of current intention.

A Goal is a target the agent is actively trying to achieve. Goals
have simple predicates (satisfied, progress) so the planner can:
1. Know when a goal is done and pop to the next one
2. Bias procedure selection toward actions that make progress
3. Explain its own behavior ("I'm collecting ingots to craft")

Goals are intentionally simple — they are NOT a planning language.
They're more like waypoints that the rule-based planner respects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import structlog

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


@dataclass
class Goal:
    """A single intention the agent is working toward.

    Fields:
    - name: stable identifier (e.g., "collect_50_iron_ingots")
    - description: human-readable ("Accumulate 50 iron ingots for crafting")
    - preferred_procedures: procedures this goal wants to run
    - forbidden_procedures: procedures that actively hurt this goal
    - deadline: unix timestamp; after this the goal auto-expires
    - created_at: unix timestamp the goal was pushed
    """

    name: str
    description: str
    preferred_procedures: set[str] = field(default_factory=set)
    forbidden_procedures: set[str] = field(default_factory=set)
    deadline: float = 0.0  # 0 = no deadline
    created_at: float = field(default_factory=time.time)
    # Predicate: returns True when the goal is satisfied.
    # Takes AgentContext, returns bool.
    # Kept as Callable so goals can use arbitrary world-state logic.
    # Default: always False (goal only removed manually or on deadline)
    is_satisfied_fn: Callable[["AgentContext"], bool] | None = None
    # Predicate: returns a progress float in [0.0, 1.0]
    progress_fn: Callable[["AgentContext"], float] | None = None

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.deadline > 0 and now >= self.deadline

    def is_satisfied(self, ctx: "AgentContext") -> bool:
        if self.is_satisfied_fn is None:
            return False
        try:
            return bool(self.is_satisfied_fn(ctx))
        except Exception as e:
            logger.warning(
                "goal_is_satisfied_error",
                goal=self.name, error=str(e),
            )
            return False

    def progress(self, ctx: "AgentContext") -> float:
        if self.progress_fn is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(self.progress_fn(ctx))))
        except Exception:
            return 0.0


class GoalStack:
    """Stack of goals the agent is working on.

    The TOP of the stack is the active goal. When it's satisfied or
    expired, it's popped and the next goal becomes active. When the
    stack is empty, the planner has no specific intention and falls
    back to its regular priority logic.
    """

    def __init__(self) -> None:
        self._stack: list[Goal] = []

    @property
    def active(self) -> Goal | None:
        return self._stack[-1] if self._stack else None

    @property
    def depth(self) -> int:
        return len(self._stack)

    def push(self, goal: Goal) -> None:
        """Push a new goal — it becomes the active one."""
        self._stack.append(goal)
        logger.info(
            "goal_pushed",
            goal=goal.name, depth=len(self._stack),
        )

    def pop(self) -> Goal | None:
        if not self._stack:
            return None
        popped = self._stack.pop()
        logger.info(
            "goal_popped",
            goal=popped.name, remaining=len(self._stack),
        )
        return popped

    def clear(self) -> None:
        if self._stack:
            logger.info("goal_stack_cleared", depth=len(self._stack))
        self._stack.clear()

    def update(self, ctx: "AgentContext") -> None:
        """Reap dead goals from the stack.

        Expiry is an unconditional contract ("after the deadline the goal
        auto-expires"), so an expired goal is removed from *anywhere* in the
        stack — a buried base goal whose deadline passed while an interrupting
        sub-goal was active must not come back to life when the sub-goal pops.
        A single ``now`` is captured so two goals sharing a deadline are
        treated identically within one tick.

        Satisfaction, by contrast, is only honoured from the *top* down:
        interrupt-and-resume integrity means a buried parent that happens to
        read as satisfied right now must stay (its world-state may flip back
        before the agent resumes it). So satisfied goals are popped only while
        they sit at the top of the stack.
        """
        now = time.time()

        # 1) Sweep expired goals from anywhere in the stack.
        survivors: list[Goal] = []
        for g in self._stack:
            if g.is_expired(now):
                logger.info("goal_expired", goal=g.name)
                continue
            survivors.append(g)
        self._stack = survivors

        # 2) Pop satisfied goals, but only consecutively from the top.
        while self._stack:
            top = self._stack[-1]
            if top.is_satisfied(ctx):
                logger.info("goal_satisfied", goal=top.name)
                self._stack.pop()
                continue
            break

    def is_preferred(self, procedure_name: str) -> bool:
        """True if the active goal prefers this procedure."""
        active = self.active
        if active is None:
            return False
        return procedure_name in active.preferred_procedures

    def is_forbidden(self, procedure_name: str) -> bool:
        """True if the active goal explicitly forbids this procedure."""
        active = self.active
        if active is None:
            return False
        return procedure_name in active.forbidden_procedures

    def snapshot(self) -> dict:
        """Diagnostic snapshot for logging / observability."""
        return {
            "depth": len(self._stack),
            "active": self.active.name if self.active else None,
            "stack": [g.name for g in self._stack],
        }


# --- Goal factories — common patterns the planner / LLM can push ---

def make_collect_iron_ingots_goal(target: int, deadline_s: float = 3600) -> Goal:
    """Goal: accumulate N iron ingots in the backpack.

    Preferred: mine_ore, smelt_ore
    Forbidden: sell_to_vendor (don't sell the ingots we're collecting)
    """
    def satisfied(ctx) -> bool:
        try:
            from anima.procedures.craft_blacksmith import _count_iron_ingots
            return _count_iron_ingots(ctx) >= target
        except Exception:
            return False

    def progress(ctx) -> float:
        try:
            from anima.procedures.craft_blacksmith import _count_iron_ingots
            return _count_iron_ingots(ctx) / max(1, target)
        except Exception:
            return 0.0

    return Goal(
        name=f"collect_{target}_iron_ingots",
        description=f"Accumulate {target} iron ingots in the backpack",
        preferred_procedures={"mine_ore", "smelt_ore", "pick_up_ore_and_smelt"},
        forbidden_procedures={"sell_to_vendor"},
        deadline=time.time() + deadline_s,
        is_satisfied_fn=satisfied,
        progress_fn=progress,
    )


def make_clear_inventory_goal(deadline_s: float = 1800) -> Goal:
    """Goal: sell all crafted items and bank colored ingots."""
    def satisfied(ctx) -> bool:
        try:
            backpack = ctx.perception.self_state.equipment.get(0x15)
            if backpack is None:
                return False
            from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
            from anima.procedures.bank_deposit import _has_colored_ingots
            crafted = sum(
                1 for it in ctx.perception.world.items.values()
                if it.container == backpack and it.graphic in CRAFTED_ITEM_GRAPHICS
            )
            return crafted == 0 and not _has_colored_ingots(ctx)
        except Exception:
            return False

    return Goal(
        name="clear_inventory",
        description="Sell crafted items and bank colored ingots",
        preferred_procedures={"sell_to_vendor", "bank_deposit"},
        forbidden_procedures={"mine_ore", "smelt_ore", "craft_blacksmith"},
        deadline=time.time() + deadline_s,
        is_satisfied_fn=satisfied,
    )


def make_deposit_gold_goal(threshold: int = 200, deadline_s: float = 1800) -> Goal:
    """Goal: deposit gold when above threshold."""
    def satisfied(ctx) -> bool:
        try:
            return ctx.perception.self_state.gold < threshold
        except Exception:
            return False

    return Goal(
        name=f"deposit_gold_above_{threshold}",
        description=f"Deposit gold above {threshold}gp",
        preferred_procedures={"bank_deposit"},
        forbidden_procedures=set(),
        deadline=time.time() + deadline_s,
        is_satisfied_fn=satisfied,
    )
