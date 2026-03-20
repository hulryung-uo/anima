"""Carpentry skill — craft wooden items via the server's crafting gump.

ServUO CraftGump button ID scheme:
  GetButtonID(type, index) = 1 + type + (index * 7)
  OnResponse: buttonID - 1, type = buttonID % 7, index = buttonID / 7

  type 0 = Show group (category)
  type 1 = Create item
  type 2 = Item details
  type 6 = Misc (EXIT=0, SMELT=1, MAKE_LAST=2, LAST_TEN=3, etc.)

  MAKE_LAST = GetButtonID(6, 2) = 1 + 6 + 14 = 21
  EXIT      = GetButtonID(6, 0) = 0 (button_id 0 closes gump)
"""

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

# Tool graphics
SAW_GRAPHICS = {0x1034, 0x1035}
DOVETAIL_SAW_GRAPHICS = {0x1028, 0x1029}
ALL_TOOL_GRAPHICS = SAW_GRAPHICS | DOVETAIL_SAW_GRAPHICS

# Material graphics
LOG_GRAPHIC = 0x1BDD
BOARD_GRAPHIC = 0x1BD7
MATERIAL_GRAPHICS = {LOG_GRAPHIC, BOARD_GRAPHIC}

CARPENTRY_SKILL_ID = 11

# Gump timing
GUMP_POLL = 0.2
GUMP_TIMEOUT = 5.0
CRAFT_WAIT = 3.5

# ServUO button IDs (pre-calculated)
BUTTON_MAKE_LAST = 21  # GetButtonID(6, 2)


def _get_button_id(btn_type: int, index: int) -> int:
    """Match ServUO's GetButtonID(type, index)."""
    return 1 + btn_type + (index * 7)


# Category/item definitions: (category_name, group_index, items)
# group_index matches ServUO CraftGroup order in DefCarpentry.cs
CRAFT_TARGETS = [
    # (display_name, group_index, item_index, min_skill, boards_needed)
    ("Barrel Staves", 0, 0, 0.0, 5),     # Other group=0, item=0
    ("Barrel Lid", 0, 1, 11.0, 4),        # Other group=0, item=1
    ("Small Crate", 2, 1, 10.0, 8),       # Containers group=2, item=1
    ("Wooden Box", 2, 0, 21.0, 10),       # Containers group=2, item=0
]


class CraftCarpentry(Skill):
    """Craft wooden items using Carpentry skill."""

    name = "craft_carpentry"
    category = "crafting"
    description = "Craft wooden items using Carpentry skill"
    required_skill = (CARPENTRY_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        # Don't craft if near weight limit
        if ss.weight_max > 0 and ss.weight >= ss.weight_max - 10:
            return False

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        bp_items = [it for it in world.items.values() if it.container == backpack]
        bp_graphics = {it.graphic for it in bp_items}

        if not (bp_graphics & ALL_TOOL_GRAPHICS):
            return False

        # Check minimum material amount (need at least 4 for cheapest recipe)
        materials = sum(
            it.amount for it in bp_items if it.graphic in MATERIAL_GRAPHICS
        )
        if materials < 4:
            return False

        skill_info = ss.skills.get(CARPENTRY_SKILL_ID)
        if skill_info is None or skill_info.value < 0.0:
            return False
        return True

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()
        backpack = ss.equipment.get(0x15)

        # Find tool
        tool = None
        for it in world.items.values():
            if it.container == backpack and it.graphic in ALL_TOOL_GRAPHICS:
                tool = it
                break
        if not tool:
            return SkillResult(success=False, reward=-1.0, message="No carpentry tool")

        # Count boards + logs available (server auto-converts logs to boards)
        boards_available = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in MATERIAL_GRAPHICS
        )

        # Pick what to craft based on skill and available boards
        skill_info = ss.skills.get(CARPENTRY_SKILL_ID)
        skill_val = skill_info.value if skill_info else 0.0

        # Find the best item to craft — check each recipe
        target = None
        best_feasible = None
        for name, grp_idx, item_idx, min_skill, boards in CRAFT_TARGETS:
            if skill_val >= min_skill:
                if boards_available >= boards:
                    target = (name, grp_idx, item_idx, boards)
                elif best_feasible is None:
                    # First item we have skill for but not enough materials
                    best_feasible = (name, boards)

        if not target:
            feed = ctx.blackboard.get("activity_feed")
            if best_feasible:
                need_name, need_boards = best_feasible
                shortage = need_boards - boards_available
                msg = (
                    f"Want to craft {need_name} but need "
                    f"{shortage} more wood (have {boards_available}, need {need_boards})"
                )
                # Signal brain to gather materials
                ctx.blackboard["skill_problem"] = msg
                ctx.blackboard["last_think_time"] = 0.0  # force rethink
                if feed:
                    feed.publish("skill", msg, importance=2)
                logger.info(
                    "carpentry_need_materials",
                    item=need_name, have=boards_available, need=need_boards,
                )
            else:
                msg = f"Carpentry skill too low ({skill_val:.0f})"
                if feed:
                    feed.publish("skill", msg, importance=1)
                logger.info("carpentry_skill_too_low", skill=skill_val)
            return SkillResult(
                success=False, reward=-0.5,
                message=msg,
            )

        target_name, grp_idx, item_idx, boards_needed = target

        # Publish intent
        feed = ctx.blackboard.get("activity_feed")
        if feed:
            feed.publish(
                "skill",
                f"Crafting {target_name} (need {boards_needed} boards, have {boards_available})",
                importance=2,
            )
        logger.info(
            "carpentry_start",
            item=target_name,
            group=grp_idx,
            item_index=item_idx,
            skill=skill_val,
            boards=boards_available,
        )

        # Step 1: Open crafting gump
        ss.gumps.clear()
        await ctx.conn.send_packet(build_double_click(tool.serial))

        gump = await self._wait_gump(ctx)
        if not gump:
            return SkillResult(success=False, reward=-1.0, message="Gump didn't open")

        # Step 2: Click category (type=0, index=grp_idx)
        cat_btn_id = _get_button_id(0, grp_idx)
        logger.debug("carpentry_click_category", group=grp_idx, button_id=cat_btn_id)

        prev_serial = gump.serial
        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(gump.serial, gump.gump_id, cat_btn_id)
        )

        # Wait for new gump
        gump = await self._wait_gump_new(ctx, prev_serial)
        if not gump:
            return SkillResult(success=False, reward=-1.0, message="Category gump didn't appear")

        # Step 3: Click create item (type=1, index=item_idx)
        create_btn_id = _get_button_id(1, item_idx)
        logger.debug("carpentry_click_create", item=target_name, button_id=create_btn_id)

        # Count materials before
        mats_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in MATERIAL_GRAPHICS
        )

        prev_serial = gump.serial
        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(gump.serial, gump.gump_id, create_btn_id)
        )

        # Step 4: Wait for server result message
        result_msg = ""
        journal_mark = time.time()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            for entry in ctx.perception.social.recent(count=5):
                if entry.timestamp < journal_mark:
                    continue
                text_lower = entry.text.lower()
                if "you create" in text_lower:
                    result_msg = "success"
                    break
                if "failed to create" in text_lower:
                    result_msg = "fail"
                    break
                if "worn out your tool" in text_lower:
                    result_msg = "tool_broke"
                    break
            if result_msg:
                break

        elapsed = (time.monotonic() - start) * 1000

        # Count materials consumed
        mats_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in MATERIAL_GRAPHICS
        )
        consumed = mats_before - mats_after

        # Close remaining gump
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

        if result_msg == "success" or consumed > 0:
            msg = f"Crafted {target_name} (used {consumed} wood)"
            logger.info("carpentry_success", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", msg, importance=2)
            return SkillResult(
                success=True, reward=5.0, message=msg,
                skill_gains=[(CARPENTRY_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        elif result_msg == "fail":
            logger.info("carpentry_fail", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", f"Failed {target_name}", importance=1)
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Failed to craft {target_name}",
                skill_gains=[(CARPENTRY_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        elif result_msg == "tool_broke":
            logger.warning("carpentry_tool_broke", item=target_name)
            if feed:
                feed.publish("skill", "Saw broke!", importance=3)
            # Signal brain to buy a new tool
            ctx.blackboard["skill_problem"] = (
                "Carpentry saw broke! Need to buy a new one from a vendor."
            )
            ctx.blackboard["last_think_time"] = 0.0  # force rethink
            return SkillResult(
                success=False, reward=-2.0,
                message="Carpentry tool broke — need to buy new saw",
                duration_ms=elapsed,
            )
        else:
            logger.warning(
                "carpentry_no_response", item=target_name,
                consumed=consumed, elapsed_ms=round(elapsed),
            )
            return SkillResult(
                success=False, reward=-0.5,
                message=f"No server response for {target_name}",
                duration_ms=elapsed,
            )

    async def _wait_gump(self, ctx: BrainContext) -> GumpData | None:
        """Wait for any gump to appear."""
        deadline = time.monotonic() + GUMP_TIMEOUT
        while time.monotonic() < deadline:
            if ctx.perception.self_state.gumps:
                return next(iter(ctx.perception.self_state.gumps.values()))
            await asyncio.sleep(GUMP_POLL)
        return None

    async def _wait_gump_new(
        self, ctx: BrainContext, prev_serial: int,
    ) -> GumpData | None:
        """Wait for a gump with a different serial than prev_serial."""
        deadline = time.monotonic() + GUMP_TIMEOUT
        while time.monotonic() < deadline:
            for g in ctx.perception.self_state.gumps.values():
                if g.serial != prev_serial:
                    return g
            await asyncio.sleep(GUMP_POLL)
        return None
