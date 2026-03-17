"""Brain: polls events and ticks the behavior tree."""

from __future__ import annotations

import structlog

from anima.action.movement import wander_action
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
from anima.perception.event_stream import GameEventType

logger = structlog.get_logger()


def _has_low_hp(ctx: BrainContext) -> bool:
    return ctx.perception.self_state.hp_percent < 30.0


def _has_pending_speech(ctx: BrainContext) -> bool:
    return bool(ctx.blackboard.get("pending_speech"))


async def _flee_action(ctx: BrainContext) -> Status:
    """Placeholder flee action — just log for now."""
    logger.warning(
        "flee_triggered",
        hp=ctx.perception.self_state.hits,
        hp_max=ctx.perception.self_state.hits_max,
    )
    return Status.SUCCESS


def build_default_tree() -> Node:
    """Build the default behavior tree.

    Selector
    +-- Sequence [Survival] -- HP<30% -> flee
    +-- Sequence [Social]   -- speech heard -> respond
    +-- Action [Wander]     -- smart wander via pathfinding
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
            Action("wander", wander_action),
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
