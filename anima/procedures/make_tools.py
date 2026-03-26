"""MakeTools procedure — craft tools using tinkering gump."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import wait_for_gump, click_gump_button
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

SHOVEL_GRAPHICS = {0x0F39}

# Map craft target name → item graphics to count
_CRAFT_TARGET_GRAPHICS: dict[str, set[int]] = {
    "Pickaxe": PICKAXE_GRAPHICS,
    "Hatchet": HATCHET_GRAPHICS,
    "Tinker's Tools": TINKER_TOOLS_GRAPHICS,
    "Shovel": SHOVEL_GRAPHICS,
}


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

    def _decide_craft_target(self, ctx: AgentContext) -> str | None:
        """Decide what to craft based on what's missing. Returns gump label text."""
        if not find_in_backpack(ctx, PICKAXE_GRAPHICS):
            return "Pickaxe"
        if not find_in_backpack(ctx, HATCHET_GRAPHICS):
            return "Hatchet"
        if len(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)) < 2:
            return "Tinker's Tools"
        return None

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

        craft_target = self._decide_craft_target(ctx)
        if not craft_target:
            return ProcedureResult(success=True, message="All tools available")

        tool_serial = tools[0].serial

        # 1. Open tinkering gump by double-clicking tinker tools
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

        # 2. Click "Tools" category using text-based button matching
        tools_btn = gump.find_button_near_text("Tools")
        if tools_btn is None:
            # Fallback: button ID 1 is typically "Tools" in ServUO gumps
            tools_btn = gump.find_button_by_id(1)
        if tools_btn is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tools category button not found in gump",
            )

        result = await click_gump_button(ctx, gump, tools_btn.button_id)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="failed to select tools category",
            )

        # 3. Wait for updated gump with the tool list
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tools list gump did not appear",
            )

        gump = result.data["gump"]

        # 4. Click the specific tool to craft
        item_btn = gump.find_button_near_text(craft_target)
        if item_btn is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{craft_target}' not found in gump",
            )

        # Count target items before crafting
        target_graphics = _CRAFT_TARGET_GRAPHICS.get(craft_target, set())
        items_before = count_items(ctx, target_graphics) if target_graphics else 0

        result = await click_gump_button(ctx, gump, item_btn.button_id)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"failed to click {craft_target}",
            )

        # 5. Wait for crafting — server sends a new gump after crafting
        import asyncio
        await asyncio.sleep(3.0)

        # Check success by inventory change
        items_after = count_items(ctx, target_graphics) if target_graphics else 0
        if items_after > items_before:
            logger.info("make_tools_crafted", item=craft_target)
            return ProcedureResult(
                success=True,
                message=f"Crafted {craft_target}",
                next_suggestion="make_tools",
            )

        # Also check journal as fallback
        journal = ctx.perception.social.recent(count=5)
        for entry in journal:
            tl = entry.text.lower()
            if "you create" in tl:
                logger.info("make_tools_crafted_journal", item=craft_target)
                return ProcedureResult(
                    success=True,
                    message=f"Crafted {craft_target}",
                    next_suggestion="make_tools",
                )
            if "you fail" in tl or "you don't have" in tl:
                logger.info("make_tools_craft_failed", item=craft_target, journal=entry.text)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"Craft failed: {entry.text}",
                )

        # Track consecutive failures — give up after 5 to prevent loop
        fail_count = ctx.blackboard.get("_make_tools_fails", 0) + 1
        ctx.blackboard["_make_tools_fails"] = fail_count
        if fail_count >= 5:
            ctx.blackboard["_make_tools_fails"] = 0
            logger.warning("make_tools_gave_up", item=craft_target, fails=fail_count)
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Gave up crafting {craft_target} after {fail_count} attempts",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Craft result unclear for {craft_target} ({fail_count}/5)",
        )
