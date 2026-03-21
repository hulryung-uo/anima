"""Blacksmith skill — forge weapons and armor from ingots via crafting gump.

Uses the same ServUO CraftGump button scheme as carpentry:
  GetButtonID(type, index) = 1 + type + (index * 7)
  type 0 = Show group (category)
  type 1 = Create item
  MAKE_LAST = GetButtonID(6, 2) = 21

Requires proximity to both an anvil AND a forge (within 2 tiles).
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

# Tool graphics — smith hammer / tongs
SMITH_HAMMER_GRAPHICS = {0x13E3, 0x13E4}  # SmithHammer
TONGS_GRAPHICS = {0x0FBB, 0x0FBC}         # Tongs
ALL_TOOL_GRAPHICS = SMITH_HAMMER_GRAPHICS | TONGS_GRAPHICS

# Material: ingots
INGOT_GRAPHIC = 0x1BF2
INGOT_GRAPHICS = {0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1}

BLACKSMITH_SKILL_ID = 7

# Anvil item/static IDs (from DefBlacksmithy.cs CheckAnvilAndForge)
ANVIL_IDS = {0x0FAF, 0x0FB0, 0x2DD5, 0x2DD6}
# Forge item/static IDs
FORGE_IDS = {0x0FB1} | set(range(0x197A, 0x19AA)) | {0x2DD8, 0xA531, 0xA535}

# Gump timing
GUMP_POLL = 0.2
GUMP_TIMEOUT = 5.0


def _get_button_id(btn_type: int, index: int) -> int:
    return 1 + btn_type + (index * 7)


# (display_name, group_index, item_index, min_skill, ingots_needed)
# Group order from ServUO DefBlacksmithy.cs:
# 0=Metal Armor, 1=Helmets, 2=Shields, 3=Bladed, 4=Axes, 5=Polearms,
# 6=Bashing, 7=Ringmail, 8=Chainmail, 9=Platemail
CRAFT_TARGETS = [
    # Ringmail — low skill, good for training
    ("Ringmail Gloves", 7, 0, 12.0, 10),
    ("Ringmail Leggings", 7, 1, 19.4, 16),
    ("Ringmail Sleeves", 7, 2, 16.9, 14),
    ("Ringmail Tunic", 7, 3, 21.9, 18),
    # Chainmail — mid skill
    ("Chainmail Coif", 8, 0, 14.5, 10),
    ("Chainmail Leggings", 8, 1, 36.7, 18),
    ("Chainmail Tunic", 8, 2, 39.1, 20),
    # Bladed weapons — varied skill
    ("Cutlass", 3, 0, 24.3, 8),
    ("Katana", 3, 1, 44.1, 8),
    ("Kryss", 3, 2, 36.7, 8),
    ("Broadsword", 3, 3, 35.4, 10),
    ("Longsword", 3, 4, 28.0, 12),
    ("Scimitar", 3, 5, 31.7, 10),
    # Platemail — high skill
    ("Plate Gorget", 9, 0, 56.4, 10),
    ("Plate Gloves", 9, 1, 58.9, 12),
    ("Plate Helm", 9, 2, 62.6, 15),
    ("Plate Arms", 9, 3, 66.3, 18),
    ("Plate Legs", 9, 4, 68.8, 20),
    ("Plate Chest", 9, 5, 75.0, 25),
    # Shields
    ("Buckler", 2, 0, 0.0, 10),
    ("Bronze Shield", 2, 1, 0.0, 12),
    ("Metal Shield", 2, 3, 0.0, 14),
    ("Metal Kite Shield", 2, 4, 4.6, 16),
    ("Heater Shield", 2, 5, 24.3, 18),
]

# Sort by min_skill ascending
CRAFT_TARGETS.sort(key=lambda t: t[3])


class CraftBlacksmith(Skill):
    """Forge weapons and armor from ingots using Blacksmith skill."""

    name = "craft_blacksmith"
    category = "crafting"
    description = "Forge weapons and armor from ingots at an anvil."
    required_skill = (BLACKSMITH_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        if ss.weight_max > 0 and ss.weight >= ss.weight_max - 10:
            return False

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        bp_items = [it for it in world.items.values() if it.container == backpack]
        bp_graphics = {it.graphic for it in bp_items}

        if not (bp_graphics & ALL_TOOL_GRAPHICS):
            return False

        # Need at least 8 ingots for cheapest recipe
        ingots = sum(
            it.amount for it in bp_items if it.graphic in INGOT_GRAPHICS
        )
        if ingots < 8:
            return False

        skill_info = ss.skills.get(BLACKSMITH_SKILL_ID)
        if skill_info is None or skill_info.value < 0.0:
            return False

        # Must be near both an anvil AND a forge (within 2 tiles)
        if not self._has_anvil_and_forge(ctx):
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
            return SkillResult(success=False, reward=-1.0, message="No smith hammer")

        # Count ingots
        ingots_available = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        # Pick what to craft
        skill_info = ss.skills.get(BLACKSMITH_SKILL_ID)
        skill_val = skill_info.value if skill_info else 0.0

        target = None
        for name, grp_idx, item_idx, min_skill, ingots in CRAFT_TARGETS:
            if skill_val >= min_skill and ingots_available >= ingots:
                target = (name, grp_idx, item_idx, ingots)

        if not target:
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Not enough ingots ({ingots_available}) or skill too low",
            )

        target_name, grp_idx, item_idx, ingots_needed = target

        feed = ctx.blackboard.get("activity_feed")
        if feed:
            feed.publish(
                "skill",
                f"Forging {target_name} ({ingots_needed} ingots)",
                importance=2,
            )
        logger.info(
            "blacksmith_start",
            item=target_name, group=grp_idx, item_index=item_idx,
            skill=skill_val, ingots=ingots_available,
        )

        # Step 1: Open crafting gump
        ss.gumps.clear()
        await ctx.conn.send_packet(build_double_click(tool.serial))

        gump = await self._wait_gump(ctx)
        if not gump:
            return SkillResult(success=False, reward=-1.0, message="Gump didn't open")

        # Step 2: Click category
        cat_btn_id = _get_button_id(0, grp_idx)
        prev_serial = gump.serial
        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(gump.serial, gump.gump_id, cat_btn_id)
        )

        gump = await self._wait_gump_new(ctx, prev_serial)
        if not gump:
            return SkillResult(success=False, reward=-1.0, message="Category gump didn't appear")

        # Step 3: Click create item
        create_btn_id = _get_button_id(1, item_idx)

        ingots_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        prev_serial = gump.serial
        ss.gumps.pop(gump.gump_id, None)
        await ctx.conn.send_packet(
            build_gump_response(gump.serial, gump.gump_id, create_btn_id)
        )

        # Step 4: Wait for result
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

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )
        consumed = ingots_before - ingots_after

        # Close remaining gump
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

        if result_msg == "success" or consumed > 0:
            msg = f"Forged {target_name} (used {consumed} ingots)"
            logger.info("blacksmith_success", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", msg, importance=2)
            return SkillResult(
                success=True, reward=5.0, message=msg,
                skill_gains=[(BLACKSMITH_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        elif result_msg == "fail":
            logger.info("blacksmith_fail", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", f"Failed {target_name}", importance=1)
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Failed to forge {target_name}",
                skill_gains=[(BLACKSMITH_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        elif result_msg == "tool_broke":
            logger.warning("blacksmith_tool_broke")
            if feed:
                feed.publish("skill", "Smith hammer broke!", importance=3)
            ctx.blackboard["skill_problem"] = (
                "Smith hammer broke! Need to buy or craft a new one."
            )
            ctx.blackboard["last_think_time"] = 0.0
            return SkillResult(
                success=False, reward=-2.0,
                message="Smith hammer broke — need new tool",
                duration_ms=elapsed,
            )
        else:
            logger.warning("blacksmith_no_response", item=target_name, consumed=consumed)
            return SkillResult(
                success=False, reward=-0.5,
                message=f"No server response for {target_name}",
                duration_ms=elapsed,
            )

    def _has_anvil_and_forge(self, ctx: BrainContext) -> bool:
        """Check that both an anvil and a forge are within 2 tiles."""
        ss = ctx.perception.self_state
        world = ctx.perception.world

        has_anvil = False
        has_forge = False

        # Check dynamic world items
        for it in world.nearby_items(ss.x, ss.y, distance=2):
            if it.graphic in ANVIL_IDS:
                has_anvil = True
            if it.graphic in FORGE_IDS:
                has_forge = True
            if has_anvil and has_forge:
                return True

        # Check map statics
        if ctx.map_reader is not None:
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    tile = ctx.map_reader.get_tile(ss.x + dx, ss.y + dy)
                    for s in tile.statics:
                        if s.graphic in ANVIL_IDS:
                            has_anvil = True
                        if s.graphic in FORGE_IDS:
                            has_forge = True
                        if has_anvil and has_forge:
                            return True

        return False

    async def _wait_gump(self, ctx: BrainContext) -> GumpData | None:
        deadline = time.monotonic() + GUMP_TIMEOUT
        while time.monotonic() < deadline:
            if ctx.perception.self_state.gumps:
                return next(iter(ctx.perception.self_state.gumps.values()))
            await asyncio.sleep(GUMP_POLL)
        return None

    async def _wait_gump_new(
        self, ctx: BrainContext, prev_serial: int,
    ) -> GumpData | None:
        deadline = time.monotonic() + GUMP_TIMEOUT
        while time.monotonic() < deadline:
            for g in ctx.perception.self_state.gumps.values():
                if g.serial != prev_serial:
                    return g
            await asyncio.sleep(GUMP_POLL)
        return None
