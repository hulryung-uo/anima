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
BOARD_GRAPHIC = 0x1BD7
# A log stack flips between TWO graphic IDs depending on stack size (0x1BDD
# and 0x1BE0) — every other module already keys on both (lumber.LOG_GRAPHICS,
# make_boards.LOG_GRAPHICS, banking/vendor KEEP sets, skills.state). Carpentry
# alone counted only 0x1BDD, so a carpenter whose logs happen to render as the
# 0x1BE0 variant saw them as zero material: can_execute gated False (materials
# < 4) with a full pack, and boards_available under-counted so _pick_target
# either chose a smaller item or fired a phantom "need more wood" shortage
# signal — stalling the whole carpentry loop. Count both stack graphics.
LOG_GRAPHICS = {0x1BDD, 0x1BE0}
LOG_GRAPHIC = 0x1BDD  # legacy single-graphic alias (kept for back-compat)
MATERIAL_GRAPHICS = LOG_GRAPHICS | {BOARD_GRAPHIC}

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


def _classify_carpentry_result(result_msg: str, consumed: int) -> str:
    """Decide the craft outcome from the journal token and material delta.

    ServUO's DefCarpentry.PlayEndingEffect sends distinct clilocs:
      1044154 "You create the item." -> success
      1044043 "You failed to create the item, and some of your materials
               are lost." -> a *failure* that STILL consumes boards
      1044157 "You failed to create the item, but no materials were lost."
      1044038 "You have worn out your tool!" -> tool_broke

    The explicit journal token must win over the ``consumed > 0`` heuristic:
    a 1044043 failure burns boards too, so ``consumed > 0`` is NOT a reliable
    success signal on its own. The old dispatch
    (``if result_msg == "success" or consumed > 0``) promoted such a failure
    to +5.0 and a fake skill gain, poisoning the reward/skill signal — exactly
    the bug fixed for blacksmithy in ``_classify_blacksmith_result``.

    ``consumed > 0`` is only promoted to ``success`` when the journal said
    nothing recognizable — that covers the quality-0 "barely able to make"
    line some shards phrase without a literal "you create".

    Returns one of: ``success`` / ``fail`` / ``tool_broke`` / ``none``.
    """
    if result_msg == "tool_broke":
        return "tool_broke"
    if result_msg == "fail":
        return "fail"
    if result_msg == "success":
        return "success"
    if consumed > 0:
        return "success"
    return "none"


# Category/item definitions: (category_name, group_index, items)
# group_index matches ServUO CraftGroup order in DefCarpentry.cs
CRAFT_TARGETS = [
    # (display_name, group_index, item_index, min_skill, boards_needed)
    # NOT sorted — _pick_target() selects the highest-min_skill feasible item
    # explicitly, so list order does not matter.
    # Group 0=Other, 1=Furniture, 2=Containers, 3=Weapons, 4=Armor, 5=Instruments
    ("Barrel Staves", 0, 0, 0.0, 5),        # Other
    ("Barrel Lid", 0, 1, 11.0, 4),          # Other
    ("Short Music Stand", 0, 2, 18.8, 8),   # Other
    ("Small Crate", 2, 1, 10.0, 8),         # Containers
    ("Wooden Box", 2, 0, 21.0, 10),         # Containers
    ("Medium Crate", 2, 2, 31.0, 15),       # Containers
    ("Wooden Bench", 1, 1, 52.6, 17),       # Furniture
    ("Large Crate", 2, 3, 47.3, 18),        # Containers
    ("Wooden Shield", 4, 0, 52.6, 9),       # Armor
    ("Quarter Staff", 3, 1, 73.6, 6),       # Weapons
    ("Shepherd's Crook", 3, 0, 78.9, 7),    # Weapons
    ("Lap Harp", 5, 0, 63.1, 20),           # Instruments
    ("Standing Harp", 5, 2, 81.5, 35),      # Instruments
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

    @staticmethod
    def _pick_target(
        skill_val: float, boards_available: int,
    ) -> tuple[tuple[str, int, int, int] | None, tuple[str, int] | None]:
        """Choose what to craft from CRAFT_TARGETS.

        Returns ``(target, best_feasible)`` where *target* is
        ``(name, group_index, item_index, boards_needed)`` for the item to
        craft now, or ``None`` if nothing is craftable with the boards on
        hand. *best_feasible* is ``(name, boards_needed)`` for the
        highest-skill item the carpenter is skilled enough for but lacks
        boards to make (used to signal a material shortage), or ``None``.

        Picks the item with the HIGHEST ``min_skill`` the carpenter qualifies
        for and can afford — higher-skill items give better skill gains and
        sell for more. CRAFT_TARGETS is not sorted, so this scans for the max
        explicitly rather than relying on list order (the old loop kept the
        last list-order match, which was not the highest-skill item — e.g. an
        80-skill carpenter crafted the 63.1-skill Lap Harp instead of the
        78.9-skill Shepherd's Crook, gaining almost no skill).
        """
        target: tuple[str, int, int, int] | None = None
        target_skill = -1.0
        best_feasible: tuple[str, int] | None = None
        best_feasible_skill = -1.0
        for name, grp_idx, item_idx, min_skill, boards in CRAFT_TARGETS:
            if skill_val < min_skill:
                continue
            if boards_available >= boards:
                if min_skill > target_skill:
                    target = (name, grp_idx, item_idx, boards)
                    target_skill = min_skill
            elif min_skill > best_feasible_skill:
                best_feasible = (name, boards)
                best_feasible_skill = min_skill
        return target, best_feasible

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

        target, best_feasible = self._pick_target(skill_val, boards_available)

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

        outcome = _classify_carpentry_result(result_msg, consumed)
        if outcome == "success":
            msg = f"Crafted {target_name} (used {consumed} wood)"
            logger.info("carpentry_success", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", msg, importance=2)
            return SkillResult(
                success=True, reward=5.0, message=msg,
                skill_gains=[(CARPENTRY_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        elif outcome == "fail":
            logger.info("carpentry_fail", item=target_name, consumed=consumed)
            if feed:
                feed.publish("skill", f"Failed {target_name}", importance=1)
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Failed to craft {target_name}",
                skill_gains=[(CARPENTRY_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        elif outcome == "tool_broke":
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
