"""CraftBlacksmith procedure — craft items at anvil using blacksmithy gump."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import click_gump_button, wait_for_gump
from anima.actions.inventory import count_items, find_in_backpack
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

TONGS_GRAPHICS = {0x0FBB, 0x0FBC}
INGOT_GRAPHIC = 0x1BF2
MIN_INGOTS = 6  # minimum for simplest weapon


class CraftBlacksmith(Procedure):
    name = "craft_blacksmith"
    description = "Craft weapons or armor at an anvil using blacksmithy skill."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, TONGS_GRAPHICS):
            return False
        return count_items(ctx, {INGOT_GRAPHIC}) >= MIN_INGOTS

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        tools = find_in_backpack(ctx, TONGS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tongs",
            )

        if count_items(ctx, {INGOT_GRAPHIC}) < MIN_INGOTS:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="not enough ingots",
            )

        # Open crafting gump
        from anima.client.packets import build_double_click
        await ctx.conn.send_packet(build_double_click(tools[0].serial))

        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="blacksmithy gump did not open",
            )

        gump = result.data["gump"]

        # Click first available category (Weapons typically)
        if not gump.buttons:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="no buttons in crafting gump",
            )

        # Use first non-zero button (skip close/cancel buttons which are 0)
        category_btn = None
        for btn in gump.buttons:
            if btn.button_id > 0:
                category_btn = btn.button_id
                break

        if category_btn is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="no valid category button",
            )

        result = await click_gump_button(ctx, gump, category_btn)
        await asyncio.sleep(2.0)

        return ProcedureResult(
            success=True,
            message="Craft command sent",
            next_suggestion="craft_blacksmith",
        )
