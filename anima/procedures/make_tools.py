"""MakeTools procedure — craft tools using tinkering gump."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import craft_via_gump, wait_for_gump, click_gump_button
from anima.actions.inventory import find_in_backpack, count_items
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.crafting.tinker import (
    INGOT_GRAPHIC,
    TINKER_TOOLS_GRAPHICS,
    PICKAXE_GRAPHICS,
    HATCHET_GRAPHICS,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Minimum ingots needed to craft a tool
MIN_INGOTS_FOR_TOOL = 4


class MakeTools(Procedure):
    name = "make_tools"
    description = "Craft tools (pickaxe, hatchet, tinker tools) using tinkering."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS):
            return False
        if count_items(ctx, {INGOT_GRAPHIC}) < MIN_INGOTS_FOR_TOOL:
            return False
        # Only craft if we're low on tools
        has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS))
        has_hatchet = bool(find_in_backpack(ctx, HATCHET_GRAPHICS))
        has_tinker = len(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)) >= 2
        return not (has_pickaxe and has_hatchet and has_tinker)

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        tools = find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tinker tools",
            )

        if count_items(ctx, {INGOT_GRAPHIC}) < MIN_INGOTS_FOR_TOOL:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="not enough ingots",
            )

        # Determine what to craft — prioritize what's missing
        # This uses the gump text-based matching from the old tinker skill
        # For now, use button IDs that match ServUO's standard tinkering gump
        # The specific button IDs will need to be verified against the server
        tool_serial = tools[0].serial

        # Open tinkering gump by double-clicking tinker tools
        from anima.client.packets import build_double_click
        await ctx.conn.send_packet(build_double_click(tool_serial))

        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tinkering gump did not open",
            )

        gump = result.data["gump"]

        # Find the "Tools" category button in the gump
        tools_button = None
        for btn in gump.buttons:
            label = gump.get_button_label(btn.button_id) if hasattr(gump, 'get_button_label') else ""
            if "tool" in label.lower():
                tools_button = btn.button_id
                break

        if tools_button is None:
            # Fallback: use common ServUO tinkering gump layout
            # Category 1 is typically "Tools" in standard gumps
            tools_button = 1

        result = await click_gump_button(ctx, gump, tools_button)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="failed to select tools category",
            )

        # Wait for updated gump with tool list
        import asyncio
        await asyncio.sleep(1.0)

        return ProcedureResult(
            success=True,
            message="Tool crafting initiated",
            next_suggestion="make_tools",
        )
