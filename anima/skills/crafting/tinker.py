"""Tinkering skill — craft tools using the gump-based crafting UI."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_gump_response
from anima.perception.gump import GumpData
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# --- Item graphic IDs ---
TINKER_TOOLS_GRAPHICS = {0x1EB8, 0x1EBC}
INGOT_GRAPHIC = 0x1BF2
HATCHET_GRAPHICS = {0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D}
PICKAXE_GRAPHICS = {0x0E85, 0x0E86}
SAW_GRAPHICS = {0x1034, 0x1035}
SEWING_KIT_GRAPHICS = {0x0F9D, 0x0F9E}

TINKERING_SKILL_ID = 37

# Crafting recipes: (gump_category_text, gump_item_text, item_graphics_set)
CRAFT_RECIPES: list[tuple[str, str, set[int]]] = [
    ("Tools", "Tinker's Tools", TINKER_TOOLS_GRAPHICS),
    ("Tools", "Hatchet", HATCHET_GRAPHICS),
    ("Tools", "Pickaxe", PICKAXE_GRAPHICS),
    ("Tools", "Saw", SAW_GRAPHICS),
    ("Tools", "Sewing Kit", SEWING_KIT_GRAPHICS),
]

# Gump interaction timeouts
GUMP_WAIT_TIMEOUT = 5.0
GUMP_POLL_INTERVAL = 0.2
CRAFT_WAIT_TIME = 3.0


# --- Gump helpers ---


async def _wait_for_gump(
    ctx: BrainContext,
    *,
    timeout: float = GUMP_WAIT_TIMEOUT,
    exclude_ids: set[int] | None = None,
) -> GumpData | None:
    """Poll self_state.gumps until a new crafting gump appears.

    Returns the GumpData if found within timeout, else None.
    """
    exclude = exclude_ids or set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for gump_id, gump in ctx.perception.self_state.gumps.items():
            if gump_id not in exclude:
                return gump
        await asyncio.sleep(GUMP_POLL_INTERVAL)
    return None


async def _wait_for_gump_update(
    ctx: BrainContext,
    gump_id: int,
    previous_layout: str,
    *,
    timeout: float = GUMP_WAIT_TIMEOUT,
) -> GumpData | None:
    """Wait for a gump with the same gump_id but different layout (server refresh).

    Returns the updated GumpData if found within timeout, else None.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gump = ctx.perception.self_state.gumps.get(gump_id)
        if gump is not None and gump.layout != previous_layout:
            return gump
        await asyncio.sleep(GUMP_POLL_INTERVAL)
    return None


async def _click_gump_button(
    ctx: BrainContext,
    gump: GumpData,
    button_text: str,
) -> bool:
    """Find and click a reply button near the given text label.

    Sends a gump response packet with the matched button_id.
    Returns True if a matching button was found and clicked.
    """
    btn = gump.find_button_near_text(button_text)
    if btn is None:
        logger.warning(
            "gump_button_not_found",
            text=button_text,
            gump_id=gump.gump_id,
        )
        return False

    await ctx.conn.send_packet(
        build_gump_response(
            serial=gump.serial,
            gump_id=gump.gump_id,
            button_id=btn.button_id,
        )
    )
    return True


async def _close_gump(ctx: BrainContext, gump: GumpData) -> None:
    """Close a gump by sending button_id=0 (cancel/close)."""
    await ctx.conn.send_packet(
        build_gump_response(
            serial=gump.serial,
            gump_id=gump.gump_id,
            button_id=0,
        )
    )
    # Remove from local state so we don't re-detect it
    ctx.perception.self_state.gumps.pop(gump.gump_id, None)


class CraftTinker(Skill):
    """Craft tools using the Tinkering skill and a gump-based crafting menu."""

    name = "craft_tinker"
    category = "crafting"
    description = "Craft tools using Tinkering skill"
    required_items = [0x1EB8]  # tinker tools (any of the graphic IDs)
    required_skill = (TINKERING_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        """Check tinker tools AND ingots exist in backpack."""
        ss = ctx.perception.self_state
        world = ctx.perception.world

        backpack = ss.equipment.get(0x15)  # Layer.BACKPACK
        if not backpack:
            return False

        backpack_items = [it for it in world.items.values() if it.container == backpack]
        backpack_graphics = {it.graphic for it in backpack_items}

        has_tools = bool(TINKER_TOOLS_GRAPHICS & backpack_graphics)
        has_ingots = INGOT_GRAPHIC in backpack_graphics

        if not has_tools or not has_ingots:
            return False

        # Check skill level
        if self.required_skill is not None:
            skill_id, min_val = self.required_skill
            skill_info = ss.skills.get(skill_id)
            if skill_info is None or skill_info.value < min_val:
                return False

        return True

    def _decide_craft_target(self, ctx: BrainContext) -> tuple[str, str, set[int]] | None:
        """Decide what to craft based on what's missing from backpack.

        Priority: tinker tools (self-replication) > hatchet > pickaxe > saw > sewing kit.
        Returns (category, item_name, graphics_set) or None.
        """
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return None

        backpack_graphics = {it.graphic for it in world.items.values() if it.container == backpack}

        for category_text, item_text, item_graphics in CRAFT_RECIPES:
            if not (item_graphics & backpack_graphics):
                return (category_text, item_text, item_graphics)

        # Everything is present — default to crafting tinker tools (always useful)
        return CRAFT_RECIPES[0]

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return SkillResult(success=False, reward=-1.0, message="No backpack")

        # 1. Find tinker tools in backpack
        tinker_tool = None
        for item in world.items.values():
            if item.container == backpack and item.graphic in TINKER_TOOLS_GRAPHICS:
                tinker_tool = item
                break

        if not tinker_tool:
            return SkillResult(success=False, reward=-1.0, message="No tinker tools")

        # 2. Decide what to craft
        target = self._decide_craft_target(ctx)
        if target is None:
            return SkillResult(success=False, reward=-1.0, message="Nothing to craft")
        category_text, item_text, _item_graphics = target

        logger.info(
            "tinker_craft_start",
            category=category_text,
            item=item_text,
        )

        # Record existing gump IDs to distinguish the new crafting gump
        existing_gump_ids = set(ss.gumps.keys())

        # Record journal size before crafting to detect new messages
        journal_before = len(ctx.perception.social.journal)

        # 3. Double-click tinker tools to open crafting gump
        await ctx.conn.send_packet(build_double_click(tinker_tool.serial))

        # 4. Wait for crafting gump to appear
        gump = await _wait_for_gump(ctx, exclude_ids=existing_gump_ids)
        if gump is None:
            return SkillResult(
                success=False,
                reward=-1.0,
                message="Crafting gump did not appear",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        logger.debug("tinker_gump_opened", gump_id=gump.gump_id)

        # 5. Click category button (e.g. "Tools")
        if not await _click_gump_button(ctx, gump, category_text):
            await _close_gump(ctx, gump)
            return SkillResult(
                success=False,
                reward=-1.0,
                message=f"Category '{category_text}' not found in gump",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # 6. Wait for gump to update with category items
        old_layout = gump.layout
        updated_gump = await _wait_for_gump_update(ctx, gump.gump_id, old_layout)
        if updated_gump is None:
            await _close_gump(ctx, gump)
            return SkillResult(
                success=False,
                reward=-1.0,
                message="Gump did not update after category click",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # 7. Click item button (e.g. "Hatchet")
        if not await _click_gump_button(ctx, updated_gump, item_text):
            await _close_gump(ctx, updated_gump)
            return SkillResult(
                success=False,
                reward=-1.0,
                message=f"Item '{item_text}' not found in gump",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # 8. Wait for crafting animation and result
        await asyncio.sleep(CRAFT_WAIT_TIME)

        # 9. Check result via system messages in the journal
        new_entries = list(ctx.perception.social.journal)[journal_before:]
        success = any("you create" in e.text.lower() for e in new_entries)
        failed = any("you fail" in e.text.lower() for e in new_entries)
        tool_broke = any(
            "broke" in e.text.lower() or "destroyed" in e.text.lower() for e in new_entries
        )

        # 10. Close gump (server may have already closed it, but try anyway)
        current_gump = ss.gumps.get(updated_gump.gump_id, updated_gump)
        await _close_gump(ctx, current_gump)

        elapsed = (time.monotonic() - start) * 1000

        if success:
            reward = 8.0
            msg = f"Crafted {item_text}"
            if tool_broke:
                msg += " (tool broke)"
                reward -= 1.0
                ctx.blackboard["skill_problem"] = (
                    "Tinker tools broke! Need to buy new ones from a vendor."
                )
                ctx.blackboard["last_think_time"] = 0.0
            logger.info("tinker_craft_success", item=item_text)
            return SkillResult(
                success=True,
                reward=reward,
                message=msg,
                skill_gains=[(TINKERING_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        elif failed:
            msg = "Crafting failed"
            if tool_broke:
                msg += " (tool broke)"
                ctx.blackboard["skill_problem"] = (
                    "Tinker tools broke! Need to buy new ones from a vendor."
                )
                ctx.blackboard["last_think_time"] = 0.0
            logger.info("tinker_craft_failed", item=item_text)
            return SkillResult(
                success=False,
                reward=-2.0,
                message=msg,
                duration_ms=elapsed,
            )
        else:
            # No recognizable message — ambiguous result
            logger.warning(
                "tinker_craft_unknown_result",
                item=item_text,
                messages=[e.text for e in new_entries],
            )
            return SkillResult(
                success=False,
                reward=-1.0,
                message="Crafting result unknown",
                duration_ms=elapsed,
            )
