"""CraftBlacksmith procedure — craft weapons/armor at anvil to sell for profit.

Uses tongs + ingots at a forge/anvil. Crafts items based on current skill level.
Success detected by inventory change (new item appears in backpack).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import wait_for_gump
from anima.actions.inventory import count_items, find_in_backpack
from anima.client.packets import build_double_click, build_gump_response
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.crafting.blacksmith import ANVIL_IDS, FORGE_IDS

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

TONGS_GRAPHICS = {0x0FBB, 0x0FBC}  # smith's hammer / tongs
INGOT_GRAPHIC = 0x1BF2
MIN_INGOTS = 8  # most weapons need 8-12 ingots

# Recipes: (category_text, item_text, ingots_needed, min_skill)
# Category names from ServUO CraftGump clilocs
_RECIPES = [
    ("Bladed", "Cutlass", 8, 24.3),
    ("Bladed", "Katana", 8, 44.1),
    ("Bladed", "Scimitar", 10, 31.7),
    ("Metal Armor", "Ringmail Gloves", 10, 12.0),
    ("Metal Armor", "Ringmail Sleeves", 14, 16.9),
    ("Metal Armor", "Ringmail Leggings", 16, 19.4),
    ("Metal Armor", "Ringmail Tunic", 18, 21.9),
]

# Graphics of items we crafted (to detect in inventory and to sell)
CRAFTED_WEAPON_GRAPHICS = {
    0x1441,  # cutlass
    0x13FF,  # katana
    0x13B6,  # scimitar
    0x0F5E,  # broadsword
}
CRAFTED_ARMOR_GRAPHICS = {
    0x13EB,  # ringmail gloves
    0x13F0,  # ringmail leggings
    0x13EE,  # ringmail sleeves
    0x13EC,  # ringmail tunic
}
CRAFTED_ITEM_GRAPHICS = CRAFTED_WEAPON_GRAPHICS | CRAFTED_ARMOR_GRAPHICS


def _has_anvil_and_forge(ctx: AgentContext) -> bool:
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


class CraftBlacksmith(Procedure):
    name = "craft_blacksmith"
    description = "Craft weapons or armor from ingots to sell for profit."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, TONGS_GRAPHICS):
            return False
        if count_items(ctx, {INGOT_GRAPHIC}) < MIN_INGOTS:
            return False
        return _has_anvil_and_forge(ctx)

    def _pick_recipe(self, ctx: AgentContext) -> tuple[str, str, int] | None:
        """Pick best recipe based on skill level and available ingots."""
        ingots = count_items(ctx, {INGOT_GRAPHIC})
        skill = 0.0
        for sk in ctx.perception.self_state.skills.values():
            if sk.id == 7:  # Blacksmithy
                skill = sk.value
                break

        for cat, item, cost, min_skill in _RECIPES:
            if skill >= min_skill and ingots >= cost:
                return cat, item, cost
        return None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state

        tools = find_in_backpack(ctx, TONGS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tongs",
            )

        if not _has_anvil_and_forge(ctx):
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no anvil or forge within 2 tiles",
            )

        recipe = self._pick_recipe(ctx)
        if not recipe:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no suitable recipe (skill too low or not enough ingots)",
            )

        category_text, item_text, ingot_cost = recipe
        logger.info(
            "craft_bs_start",
            item=item_text, category=category_text,
            ingot_cost=ingot_cost, ingots=count_items(ctx, {INGOT_GRAPHIC}),
            skill=next((sk.value for sk in ss.skills.values() if sk.id == 7), 0),
        )

        # Count items before crafting
        items_before = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15) and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])
        ingots_before = count_items(ctx, {INGOT_GRAPHIC})

        # 1. Open blacksmithy gump
        ss.gumps.clear()
        await ctx.conn.send_packet(build_double_click(tools[0].serial))
        await asyncio.sleep(0.5)

        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="blacksmithy gump did not open",
            )

        gump = result.data["gump"]

        # Log gump contents for debugging
        # 2. Click category
        cat_btn = gump.find_button_near_text(category_text)
        if not cat_btn:
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{category_text}' category not found in gump",
            )

        ss.gumps.clear()
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=cat_btn.button_id,
        ))
        await asyncio.sleep(0.5)

        # 3. Wait for item list gump
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="item list gump did not appear",
            )

        gump = result.data["gump"]

        # 4. Click specific item to craft
        item_btn = gump.find_button_near_text(item_text)
        if not item_btn:
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{item_text}' not found in gump",
            )

        ss.gumps.clear()
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=item_btn.button_id,
        ))

        # 5. Wait for crafting result
        await asyncio.sleep(4.0)

        # Read the gump notices area for server feedback
        gump_notice = ""
        for g in ss.gumps.values():
            for label in getattr(g, "labels", []):
                text = label if isinstance(label, str) else str(label)
                # Notices area is around y=295 in the blacksmith gump
                if "295)=" in text:
                    gump_notice = text.split("=", 1)[1] if "=" in text else text
                    break
            if gump_notice:
                break

        # Check success: new crafted item in backpack OR ingots decreased
        items_after = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15) and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])
        ingots_after = count_items(ctx, {INGOT_GRAPHIC})

        if items_after > items_before:
            logger.info("craft_blacksmith_success", item=item_text, ingots_used=ingots_before - ingots_after)
            ctx.blackboard["_craft_bs_fails"] = 0
            ss.gumps.clear()
            return ProcedureResult(
                success=True,
                message=f"Crafted {item_text}",
                next_suggestion="craft_blacksmith",
                details={"item": item_text},
            )

        if ingots_after < ingots_before:
            # Ingots consumed but no item — craft failed (skill check)
            logger.info("craft_blacksmith_failed", item=item_text, ingots_lost=ingots_before - ingots_after)
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Craft {item_text} failed — lost {ingots_before - ingots_after} ingots (skill check)",
                next_suggestion="craft_blacksmith",
            )

        # --- Detect specific failure from gump notice ---
        notice_lower = gump_notice.lower()

        if "sufficient metal" in notice_lower or "sufficient material" in notice_lower:
            logger.warning(
                "craft_blacksmith_no_material",
                item=item_text, notice=gump_notice,
                ingots_counted=ingots_before, ingot_cost=ingot_cost,
            )
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"Server says insufficient metal for {item_text} "
                        f"(counted {ingots_before}, need {ingot_cost}) — {gump_notice}",
            )

        if "anvil" in notice_lower or "forge" in notice_lower:
            logger.warning("craft_blacksmith_no_station", notice=gump_notice)
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"No anvil/forge — {gump_notice}",
            )

        if "skill" in notice_lower:
            logger.warning("craft_blacksmith_low_skill", item=item_text, notice=gump_notice)
            ss.gumps.clear()
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Skill too low for {item_text} — {gump_notice}",
            )

        # Nothing changed and no recognizable notice — unclear
        fail_count = ctx.blackboard.get("_craft_bs_fails", 0) + 1
        ctx.blackboard["_craft_bs_fails"] = fail_count
        ss.gumps.clear()

        logger.warning(
            "craft_blacksmith_unclear",
            item=item_text, fail_count=fail_count,
            notice=gump_notice or "(none)",
            ingots_before=ingots_before, ingots_after=ingots_after,
            items_before=items_before, items_after=items_after,
        )

        if fail_count >= 5:
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Gave up crafting after {fail_count} unclear results"
                        f" — last notice: {gump_notice or 'none'}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Craft result unclear ({fail_count}/5)"
                    f" — notice: {gump_notice or 'none'}",
        )
