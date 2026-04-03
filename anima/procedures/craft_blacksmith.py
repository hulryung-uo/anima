"""CraftBlacksmith procedure — craft weapons/armor at anvil to sell for profit.

Uses tongs + ingots at a forge/anvil. Crafts items based on current skill level.
Uses computed ServUO button IDs (not text matching) for reliable gump navigation.
"""

from __future__ import annotations

import asyncio
import re
import time
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


def _get_button_id(btn_type: int, index: int) -> int:
    """ServUO CraftGump button ID formula: 1 + type + (index * 7).

    type 0 = show group (category), type 1 = create item.
    """
    return 1 + btn_type + (index * 7)


# Recipes: (item_name, group_index, item_index, ingots_needed, min_skill)
# Group indices from ServUO DefBlacksmithy.cs:
# 0=Metal Armor, 1=Helmets, 2=Shields, 3=Bladed, 4=Axes, 5=Polearms,
# 6=Bashing, 7=Ringmail, 8=Chainmail, 9=Platemail
_RECIPES = [
    ("Cutlass", 3, 0, 8, 24.3),
    ("Katana", 3, 1, 8, 44.1),
    ("Scimitar", 3, 5, 10, 31.7),
    ("Ringmail Gloves", 7, 0, 10, 12.0),
    ("Ringmail Sleeves", 7, 2, 14, 16.9),
    ("Ringmail Leggings", 7, 1, 16, 19.4),
    ("Ringmail Tunic", 7, 3, 18, 21.9),
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

    def _pick_recipe(self, ctx: AgentContext) -> tuple[str, int, int, int] | None:
        """Pick best recipe based on skill level and available ingots.

        Returns (item_name, group_index, item_index, ingot_cost) or None.
        """
        ingots = count_items(ctx, {INGOT_GRAPHIC})
        skill = 0.0
        for sk in ctx.perception.self_state.skills.values():
            if sk.id == 7:  # Blacksmithy
                skill = sk.value
                break

        for item_name, grp_idx, item_idx, cost, min_skill in _RECIPES:
            if skill >= min_skill and ingots >= cost:
                return item_name, grp_idx, item_idx, cost
        return None

    async def _close_all_gumps(self, ctx: AgentContext) -> None:
        """Close all open gumps to prevent stale state on the server."""
        ss = ctx.perception.self_state
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state

        # Close stale gumps from previous failed runs
        await self._close_all_gumps(ctx)

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

        item_name, grp_idx, item_idx, ingot_cost = recipe
        logger.info(
            "craft_bs_start",
            item=item_name, group=grp_idx, item_index=item_idx,
            ingot_cost=ingot_cost, ingots=count_items(ctx, {INGOT_GRAPHIC}),
            skill=next((sk.value for sk in ss.skills.values() if sk.id == 7), 0),
        )

        # Count items before crafting
        items_before = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15) and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])
        ingots_before = count_items(ctx, {INGOT_GRAPHIC})
        journal_mark = time.time()

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

        # 2. Click category using computed ServUO button ID
        cat_btn_id = _get_button_id(0, grp_idx)
        if not gump.find_button_by_id(cat_btn_id):
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"category button {cat_btn_id} (group {grp_idx}) not in gump",
            )

        ss.gumps.clear()
        switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
        text_entries = [(te.entry_id, te.initial_text) for te in gump.text_entries]
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=cat_btn_id,
            switches=switches,
            text_entries=text_entries,
        ))
        await asyncio.sleep(0.5)

        # 3. Wait for item list gump
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="item list gump did not appear",
            )

        gump = result.data["gump"]

        # 4. Click specific item using computed ServUO button ID
        create_btn_id = _get_button_id(1, item_idx)
        if not gump.find_button_by_id(create_btn_id):
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"item button {create_btn_id} (index {item_idx}) not in gump",
            )

        ss.gumps.clear()
        switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
        text_entries = [(te.entry_id, te.initial_text) for te in gump.text_entries]
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=create_btn_id,
            switches=switches,
            text_entries=text_entries,
        ))

        # 5. Wait for crafting result via journal (like the skill version)
        result_msg = ""
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
                if "failed to create" in text_lower or "you fail" in text_lower:
                    result_msg = "fail"
                    break
                if "worn out your tool" in text_lower:
                    result_msg = "tool_broke"
                    break
            if result_msg:
                break

        ingots_after = count_items(ctx, {INGOT_GRAPHIC})
        items_after = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15) and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])

        # Close remaining gumps properly
        await self._close_all_gumps(ctx)

        if result_msg == "success" or items_after > items_before:
            consumed = ingots_before - ingots_after
            logger.info("craft_blacksmith_success", item=item_name, ingots_used=consumed)
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=True,
                message=f"Crafted {item_name}",
                next_suggestion="craft_blacksmith",
                details={"item": item_name},
            )

        if result_msg == "fail" or ingots_after < ingots_before:
            lost = ingots_before - ingots_after
            logger.info("craft_blacksmith_failed", item=item_name, ingots_lost=lost)
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Craft {item_name} failed — lost {lost} ingots (skill check)",
                next_suggestion="craft_blacksmith",
            )

        if result_msg == "tool_broke":
            logger.warning("craft_blacksmith_tool_broke")
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="Tongs broke — need replacement tool",
            )

        # Read gump notice from result gump text (notice area around y=295)
        gump_notice = ""
        for g in ss.gumps.values():
            for t in g.texts:
                if 280 <= t.y <= 310:
                    text = g.get_text(t.text_id)
                    if text:
                        gump_notice = re.sub(r"<[^>]+>", "", text)
                        break
            if gump_notice:
                break

        notice_lower = gump_notice.lower()

        if "sufficient metal" in notice_lower or "sufficient material" in notice_lower:
            logger.warning(
                "craft_blacksmith_no_material",
                item=item_name, notice=gump_notice,
                ingots_counted=ingots_before, ingot_cost=ingot_cost,
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"Server says insufficient metal for {item_name} "
                        f"(counted {ingots_before}, need {ingot_cost}) — {gump_notice}",
            )

        if "anvil" in notice_lower or "forge" in notice_lower:
            logger.warning("craft_blacksmith_no_station", notice=gump_notice)
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"No anvil/forge — {gump_notice}",
            )

        # Nothing changed and no recognizable result — unclear
        fail_count = ctx.blackboard.get("_craft_bs_fails", 0) + 1
        ctx.blackboard["_craft_bs_fails"] = fail_count

        logger.warning(
            "craft_blacksmith_unclear",
            item=item_name, fail_count=fail_count,
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
