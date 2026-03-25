"""ChopWood procedure — use hatchet on trees to gather logs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.target import use_on_target
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.gathering.lumber import (
    HATCHET_GRAPHICS,
    LOG_GRAPHICS,
    _find_nearby_tree,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class ChopWood(Procedure):
    name = "chop_wood"
    description = "Use hatchet on a tree to chop wood."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, HATCHET_GRAPHICS):
            return False
        return _find_nearby_tree(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)

        tools = find_in_backpack(ctx, HATCHET_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no hatchet",
            )

        tree_info = _find_nearby_tree(ctx)
        if tree_info is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no tree nearby",
            )

        tx, ty, tz, graphic = tree_info

        # Count logs before
        logs_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )

        result = await use_on_target(
            ctx, tools[0].serial,
            x=tx, y=ty, z=tz,
            graphic=graphic,
        )
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        await asyncio.sleep(3.0)

        logs_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )
        logs_gained = logs_after - logs_before

        if logs_gained > 0:
            return ProcedureResult(
                success=True,
                message=f"Chopped {logs_gained} logs",
                next_suggestion="chop_wood",
                details={"logs": logs_gained},
            )
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Failed to chop wood",
            )
