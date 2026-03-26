"""MakeTools procedure — craft tools using tinkering gump.

Uses text-based button lookup (find_button_near_text) to find the correct
gump buttons, matching the approach used by the CraftTinker skill.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import wait_for_gump
from anima.actions.inventory import find_in_backpack, count_items
from anima.client.packets import build_gump_response
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

# Map craft target → (gump category text, item graphics)
_CRAFT_TARGETS: dict[str, tuple[str, set[int]]] = {
    "Tinker's Tools": ("Tools", TINKER_TOOLS_GRAPHICS),
    "Hatchet": ("Tools", HATCHET_GRAPHICS),
    "Pickaxe": ("Tools", PICKAXE_GRAPHICS),
    "Shovel": ("Tools", SHOVEL_GRAPHICS),
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
        """Decide what to craft based on what's missing. Returns item name."""
        if not find_in_backpack(ctx, PICKAXE_GRAPHICS):
            return "Pickaxe"
        if not find_in_backpack(ctx, HATCHET_GRAPHICS):
            return "Hatchet"
        if len(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)) < 2:
            return "Tinker's Tools"
        return None

    async def _close_all_gumps(self, ctx: AgentContext) -> None:
        """Close all open gumps to prevent stale state."""
        ss = ctx.perception.self_state
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        # Close stale gumps from previous failed runs
        await self._close_all_gumps(ctx)

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

        target_entry = _CRAFT_TARGETS.get(craft_target)
        if not target_entry:
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Unknown craft target: {craft_target}",
            )
        category_text, target_graphics = target_entry

        tool_serial = tools[0].serial
        ss = ctx.perception.self_state

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

        # 2. Click category (e.g. "Tools") using text-based button lookup
        cat_btn = gump.find_button_near_text(category_text)
        if not cat_btn:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{category_text}' category not found in gump",
            )

        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(
                serial=gump.serial,
                gump_id=gump.gump_id,
                button_id=cat_btn.button_id,
            )
        )

        # 3. Wait for updated gump with the item list
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tools list gump did not appear",
            )

        gump = result.data["gump"]

        # Count target items before crafting
        items_before = count_items(ctx, target_graphics)

        # 4. Click craft target using text-based button lookup
        item_btn = gump.find_button_near_text(craft_target)
        if not item_btn:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{craft_target}' not found in gump",
            )

        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(
                serial=gump.serial,
                gump_id=gump.gump_id,
                button_id=item_btn.button_id,
            )
        )

        # 5. Wait for crafting result
        await asyncio.sleep(3.0)

        # Check success by inventory change
        items_after = count_items(ctx, target_graphics)
        if items_after > items_before:
            ctx.blackboard.pop("_make_tools_fails", None)
            logger.info("make_tools_crafted", item=craft_target)
            await self._close_all_gumps(ctx)
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
                ctx.blackboard.pop("_make_tools_fails", None)
                logger.info("make_tools_crafted_journal", item=craft_target)
                await self._close_all_gumps(ctx)
                return ProcedureResult(
                    success=True,
                    message=f"Crafted {craft_target}",
                    next_suggestion="make_tools",
                )
            if "you fail" in tl or "you don't have" in tl:
                logger.info("make_tools_craft_failed", item=craft_target, journal=entry.text)
                await self._close_all_gumps(ctx)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"Craft failed: {entry.text}",
                )

        # Track consecutive failures — give up after 5 to prevent loop
        fail_count = ctx.blackboard.get("_make_tools_fails", 0) + 1
        ctx.blackboard["_make_tools_fails"] = fail_count
        await self._close_all_gumps(ctx)
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
