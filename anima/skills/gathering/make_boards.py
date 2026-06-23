"""Convert logs to boards — use hatchet on logs in backpack."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_target_response
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

HATCHET_GRAPHICS = {0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D}
LOG_GRAPHICS = {0x1BDD, 0x1BE0}
# A board stack flips its graphic ID between 0x1BD7 and 0x1BDA depending on
# orientation/stack — ServUO marks Board with ``[FlipableAttribute(0x1BD7,
# 0x1BDA)]`` (Scripts/Items/Resource/Board.cs), exactly the way a log stack
# flips between 0x1BDD and 0x1BE0. The before/after board delta below is the
# ONLY success signal this skill has, but it used a single ``BOARD_GRAPHIC ==
# 0x1BD7`` equality test: when the freshly-made boards render as the 0x1BDA
# flip variant, ``boards_after`` counts zero, ``gained <= 0``, and a genuine,
# completed conversion is reported as ``success=False, reward=-0.5`` — the same
# inverted-reward bug already fixed for logs everywhere else (carpentry
# be07cbd, vendor KEEP 1d4e5cf). The agent then loops re-running a "failing"
# conversion that actually worked, poisoning the gathering reward signal. Count
# both stack graphics, mirroring LOG_GRAPHICS.
BOARD_GRAPHICS = {0x1BD7, 0x1BDA}
BOARD_GRAPHIC = 0x1BD7  # legacy single-graphic alias (kept for back-compat)
LUMBERJACK_SKILL_ID = 44


def _count_boards(world, backpack: int | None) -> int:
    """Sum every board stack in ``backpack``, across both flip graphics."""
    if not backpack:
        return 0
    return sum(
        it.amount for it in world.items.values()
        if it.container == backpack and it.graphic in BOARD_GRAPHICS
    )


class MakeBoards(Skill):
    """Convert logs to boards by using a hatchet on logs."""

    name = "make_boards"
    category = "crafting"
    description = "Use hatchet on logs to make boards."
    required_skill = (LUMBERJACK_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        # Boards are lighter than logs, but still check weight
        if ss.weight_max > 0 and ss.weight >= ss.weight_max - 10:
            return False
        if not _find_hatchet(ctx):
            return False
        return _count_logs(ctx) >= 1

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)
        start = time.monotonic()

        hatchet = _find_hatchet(ctx)
        if not hatchet:
            return SkillResult(success=False, reward=-1.0, message="No hatchet")

        log_item = _find_log(ctx)
        if not log_item:
            return SkillResult(success=False, reward=-1.0, message="No logs")

        boards_before = _count_boards(world, backpack)

        logger.info(
            "make_boards_start",
            logs=log_item.amount,
            log_serial=f"0x{log_item.serial:08X}",
        )
        feed = ctx.blackboard.get("activity_feed")
        if feed:
            feed.publish("skill", f"Converting {log_item.amount} logs to boards", importance=2)

        # Double-click hatchet → target cursor
        ss.pending_target = None
        await ctx.conn.send_packet(build_double_click(hatchet.serial))

        # Wait for target cursor
        for _ in range(20):
            await asyncio.sleep(0.1)
            if ss.pending_target is not None:
                break

        if ss.pending_target is None:
            return SkillResult(success=False, reward=-0.5, message="No target cursor")

        cursor_id = ss.pending_target.get("cursor_id", 0)
        ss.pending_target = None

        # Target the logs in backpack (object target = target_type 0, serial only)
        await ctx.conn.send_packet(build_target_response(
            target_type=0, cursor_id=cursor_id,
            serial=log_item.serial,
        ))

        # Wait for conversion
        await asyncio.sleep(1.5)

        boards_after = _count_boards(world, backpack)
        gained = boards_after - boards_before
        elapsed = (time.monotonic() - start) * 1000

        if gained > 0:
            logger.info("make_boards_success", boards=gained)
            if feed:
                feed.publish("skill", f"Made {gained} boards!", importance=2)
            return SkillResult(
                success=True, reward=3.0 + gained,
                message=f"Made {gained} boards",
                duration_ms=elapsed,
            )
        else:
            logger.info("make_boards_no_result")
            return SkillResult(
                success=False, reward=-0.5,
                message="No boards produced",
                duration_ms=elapsed,
            )


def _find_hatchet(ctx: "BrainContext"):
    """Find hatchet in backpack or equipped."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)
    if backpack:
        for it in world.items.values():
            if it.container == backpack and it.graphic in HATCHET_GRAPHICS:
                return it
    for layer in (0x01, 0x02):
        eq = ss.equipment.get(layer)
        if eq:
            it = world.items.get(eq)
            if it and it.graphic in HATCHET_GRAPHICS:
                return it
    return None


def _find_log(ctx: "BrainContext"):
    """Find logs in backpack."""
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return None
    for it in ctx.perception.world.items.values():
        if it.container == backpack and it.graphic in LOG_GRAPHICS:
            return it
    return None


def _count_logs(ctx: "BrainContext") -> int:
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return 0
    return sum(
        it.amount for it in ctx.perception.world.items.values()
        if it.container == backpack and it.graphic in LOG_GRAPHICS
    )
