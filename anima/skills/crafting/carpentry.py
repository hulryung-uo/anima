"""Carpentry skill — craft wooden items from logs using the gump-based crafting UI."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_gump_response
from anima.perception.gump import GumpData
from anima.perception.world_state import ItemInfo
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Tool graphics
SAW_GRAPHICS = {0x1034, 0x1035}  # Saw
DOVETAIL_SAW_GRAPHICS = {0x1028, 0x1029}  # Dovetail Saw
ALL_TOOL_GRAPHICS = SAW_GRAPHICS | DOVETAIL_SAW_GRAPHICS

# Material graphics
LOG_GRAPHIC = 0x1BDD
BOARD_GRAPHIC = 0x1BD7
MATERIAL_GRAPHICS = {LOG_GRAPHIC, BOARD_GRAPHIC}

CARPENTRY_SKILL_ID = 11

# Gump polling settings
GUMP_POLL_INTERVAL = 0.2
GUMP_TIMEOUT = 5.0
CRAFT_WAIT = 3.5


@dataclass
class CraftableItem:
    """Definition of a craftable carpentry item."""

    name: str
    category: str
    min_skill: float


# Items ordered by skill difficulty (ascending) for progressive training
CRAFTABLE_ITEMS: list[CraftableItem] = [
    CraftableItem(name="Boards", category="Other", min_skill=0.0),
    CraftableItem(name="Barrel Staves", category="Other", min_skill=0.0),
    CraftableItem(name="Fishing Pole", category="Tools", min_skill=0.0),
    CraftableItem(name="Shepherd's Crook", category="Tools", min_skill=0.0),
    CraftableItem(name="Wooden Box", category="Containers", min_skill=0.0),
    CraftableItem(name="Wooden Shield", category="Armor", min_skill=0.0),
]


async def _find_backpack_item(ctx: BrainContext, graphic_ids: set[int]) -> ItemInfo | None:
    """Find the first item in the player's backpack matching any of the given graphics."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return None
    for item in world.items.values():
        if item.container == backpack and item.graphic in graphic_ids:
            return item
    return None


async def _wait_for_gump(
    ctx: BrainContext,
    timeout: float = GUMP_TIMEOUT,
    exclude_layout: str = "",
) -> GumpData | None:
    """Poll self_state.gumps until a new gump appears or timeout is reached.

    If exclude_layout is set, skip gumps with identical layout (waiting for
    a server-updated version of the same gump).
    """
    ss = ctx.perception.self_state
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for gump in ss.gumps.values():
            if exclude_layout and gump.layout == exclude_layout:
                continue
            return gump
        await asyncio.sleep(GUMP_POLL_INTERVAL)
    return None


async def _click_gump_button(ctx: BrainContext, gump: GumpData, button_id: int) -> None:
    """Send a gump response clicking the specified button, then remove the gump."""
    ss = ctx.perception.self_state
    packet = build_gump_response(
        serial=gump.serial,
        gump_id=gump.gump_id,
        button_id=button_id,
    )
    # Remove gump before sending so we can detect the next one
    ss.gumps.pop(gump.gump_id, None)
    await ctx.conn.send_packet(packet)


def _pick_item_to_craft(skill_value: float) -> CraftableItem:
    """Choose the best item to craft based on current skill level.

    Start with Boards (always useful, converts logs to boards).
    Progress to harder items as skill improves.
    """
    if skill_value < 40.0:
        return CRAFTABLE_ITEMS[0]  # Boards
    if skill_value < 60.0:
        return CRAFTABLE_ITEMS[4]  # Wooden Box
    return CRAFTABLE_ITEMS[5]  # Wooden Shield


class CraftCarpentry(Skill):
    """Craft wooden items using Carpentry skill."""

    name = "craft_carpentry"
    category = "crafting"
    description = "Craft wooden items using Carpentry skill"
    required_items = [0x1034]  # saw (overridden in can_execute)
    required_skill = (CARPENTRY_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        """Check that a carpentry tool AND logs/boards exist in backpack."""
        ss = ctx.perception.self_state
        world = ctx.perception.world

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        backpack_items = [it for it in world.items.values() if it.container == backpack]
        backpack_graphics = {it.graphic for it in backpack_items}

        has_tool = bool(backpack_graphics & ALL_TOOL_GRAPHICS)
        if not has_tool:
            return False

        has_material = bool(backpack_graphics & MATERIAL_GRAPHICS)
        if not has_material:
            return False

        # Check skill requirement
        if self.required_skill is not None:
            skill_id, min_val = self.required_skill
            skill_info = ss.skills.get(skill_id)
            if skill_info is None or skill_info.value < min_val:
                return False

        return True

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        start = time.monotonic()

        # 1. Find carpentry tool in backpack
        tool = await _find_backpack_item(ctx, ALL_TOOL_GRAPHICS)
        if not tool:
            return SkillResult(success=False, reward=-1.0, message="No carpentry tool in backpack")

        # 2. Check materials
        material = await _find_backpack_item(ctx, MATERIAL_GRAPHICS)
        if not material:
            return SkillResult(success=False, reward=-1.0, message="No logs or boards in backpack")

        # 3. Decide what to craft based on skill level
        skill_info = ss.skills.get(CARPENTRY_SKILL_ID)
        skill_value = skill_info.value if skill_info else 0.0
        target_item = _pick_item_to_craft(skill_value)

        logger.info(
            "carpentry_start",
            tool_serial=hex(tool.serial),
            item=target_item.name,
            category=target_item.category,
            skill=skill_value,
        )

        # 4. Clear any existing gumps so we can detect the new one
        ss.gumps.clear()

        # 5. Double-click carpentry tool to open crafting gump
        await ctx.conn.send_packet(build_double_click(tool.serial))

        # 6. Wait for crafting gump to appear
        gump = await _wait_for_gump(ctx)
        if not gump:
            return SkillResult(
                success=False,
                reward=-1.0,
                message="Crafting gump did not appear",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        logger.debug(
            "carpentry_gump_opened",
            gump_id=hex(gump.gump_id),
            buttons=len(gump.buttons),
            texts=len(gump.text_lines),
        )

        # 7. Navigate gump: click category button
        category_btn = gump.find_button_near_text(target_item.category)
        logger.debug(
            "carpentry_category_search",
            category=target_item.category,
            found=category_btn is not None,
            button_id=category_btn.button_id if category_btn else None,
            available_texts=[gump.get_text(t.text_id) for t in gump.texts[:15]],
        )
        if not category_btn:
            # Close the gump gracefully
            await _click_gump_button(ctx, gump, 0)
            return SkillResult(
                success=False,
                reward=-1.0,
                message=f"Category '{target_item.category}' not found in gump",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        prev_layout = gump.layout
        prev_gump_id = gump.gump_id
        # Send response but DON'T pop gump — server may update it in-place
        packet = build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id, button_id=category_btn.button_id,
        )
        await ctx.conn.send_packet(packet)

        # 8. Wait for updated gump with item list (different layout or new gump)
        await asyncio.sleep(0.5)
        # Check if server sent a new gump (same or different ID)
        gump = None
        deadline = time.monotonic() + GUMP_TIMEOUT
        while time.monotonic() < deadline:
            for g in ss.gumps.values():
                if g.layout != prev_layout:
                    gump = g
                    break
            if gump:
                break
            await asyncio.sleep(GUMP_POLL_INTERVAL)
        if not gump:
            return SkillResult(
                success=False,
                reward=-1.0,
                message="Item list gump did not appear",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # 9. Click the specific item button
        item_btn = gump.find_button_near_text(target_item.name)
        logger.debug(
            "carpentry_item_search",
            item=target_item.name,
            found=item_btn is not None,
            button_id=item_btn.button_id if item_btn else None,
            available_texts=[gump.get_text(t.text_id) for t in gump.texts[:15]],
        )
        if not item_btn:
            await _click_gump_button(ctx, gump, 0)
            return SkillResult(
                success=False,
                reward=-1.0,
                message=f"Item '{target_item.name}' not found in gump",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # Count materials before crafting
        backpack = ss.equipment.get(0x15)
        mats_before = sum(
            it.amount for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic in MATERIAL_GRAPHICS
        )

        await _click_gump_button(ctx, gump, item_btn.button_id)

        # 10. Wait for crafting to complete
        await asyncio.sleep(CRAFT_WAIT)

        elapsed = (time.monotonic() - start) * 1000

        # Count materials after crafting
        mats_after = sum(
            it.amount for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic in MATERIAL_GRAPHICS
        )
        material_consumed = mats_before - mats_after

        # 11. Check result via system messages in the journal
        success_msgs = ctx.perception.social.search("You create")
        fail_msgs = ctx.perception.social.search("You fail")

        # Filter to recent messages (within last 5 seconds)
        now = time.time()
        recent_success = [e for e in success_msgs if now - e.timestamp < 5.0]
        recent_fail = [e for e in fail_msgs if now - e.timestamp < 5.0]

        if recent_success:
            logger.info(
                "carpentry_success",
                item=target_item.name,
                elapsed_ms=round(elapsed),
            )
            return SkillResult(
                success=True,
                reward=5.0,
                message=f"Crafted {target_item.name}",
                skill_gains=[(CARPENTRY_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        elif recent_fail:
            logger.info(
                "carpentry_fail",
                item=target_item.name,
                elapsed_ms=round(elapsed),
            )
            return SkillResult(
                success=False,
                reward=-1.0,
                message=f"Failed to craft {target_item.name}",
                skill_gains=[(CARPENTRY_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        else:
            # No system message — check if materials were consumed as fallback
            if material_consumed > 0:
                logger.info(
                    "carpentry_inferred_success",
                    item=target_item.name,
                    material_consumed=material_consumed,
                    elapsed_ms=round(elapsed),
                )
                return SkillResult(
                    success=True,
                    reward=4.0,
                    message=f"Crafted {target_item.name} (inferred from material use)",
                    skill_gains=[(CARPENTRY_SKILL_ID, 0.1)],
                    duration_ms=elapsed,
                )

            logger.warning(
                "carpentry_unknown_result",
                item=target_item.name,
                material_consumed=material_consumed,
                elapsed_ms=round(elapsed),
            )
            return SkillResult(
                success=False,
                reward=-0.5,
                message=f"Crafting result unknown for {target_item.name}",
                duration_ms=elapsed,
            )
