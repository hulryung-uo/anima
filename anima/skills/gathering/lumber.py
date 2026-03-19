"""Lumberjacking skill — chop trees for logs."""

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

TREE_GRAPHICS = {
    0x0CCA, 0x0CCB, 0x0CCC, 0x0CCD, 0x0CD0, 0x0CD3, 0x0CD6, 0x0CD8,
    0x0CDA, 0x0CDD, 0x0CE0, 0x0CE3, 0x0CE6, 0x0CF8, 0x0CFB, 0x0CFE,
    0x0D01, 0x0D25, 0x0D27, 0x0D35, 0x0D37, 0x0D38, 0x0D42, 0x0D43,
    0x0D58, 0x0D59, 0x0D5A, 0x0D5B, 0x0D94, 0x0D95, 0x0D96, 0x0D97,
    0x0D98, 0x0D99, 0x0D9A, 0x0D9B,
}

LOG_GRAPHICS = {0x1BDD, 0x1BE0}
LUMBERJACK_SKILL_ID = 44


class ChopWood(Skill):
    """Chop trees with a hatchet to gather logs."""

    name = "chop_wood"
    category = "gathering"
    description = "Use a hatchet on nearby trees to chop logs."
    required_skill = (LUMBERJACK_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        if not _find_hatchet(ctx):
            return False
        nearby = ctx.perception.world.nearby_items(ss.x, ss.y, distance=3)
        return any(it.graphic in TREE_GRAPHICS for it in nearby)

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        backpack = ss.equipment.get(0x15)
        hatchet = _find_hatchet(ctx)

        if not hatchet:
            return SkillResult(success=False, reward=-1.0, message="No hatchet")

        nearby = world.nearby_items(ss.x, ss.y, distance=3)
        tree = None
        for item in nearby:
            if item.graphic in TREE_GRAPHICS:
                tree = item
                break

        if not tree:
            return SkillResult(success=False, reward=-1.0, message="No trees nearby")

        logs_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )

        await ctx.conn.send_packet(build_double_click(hatchet.serial))
        await asyncio.sleep(0.5)

        await ctx.conn.send_packet(build_target_response(
            target_type=1, cursor_id=0,
            x=tree.x, y=tree.y, z=tree.z, graphic=tree.graphic,
        ))

        await asyncio.sleep(3.0)

        logs_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )

        elapsed = (time.monotonic() - start) * 1000
        logs_gained = logs_after - logs_before

        if logs_gained > 0:
            reward = 5.0 + logs_gained
            logger.info("chop_success", logs=logs_gained)
            return SkillResult(
                success=True, reward=reward,
                message=f"Chopped {logs_gained} logs",
                skill_gains=[(LUMBERJACK_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        else:
            return SkillResult(
                success=False, reward=-1.0,
                message="No logs obtained",
                duration_ms=elapsed,
            )


def _find_hatchet(ctx: BrainContext):
    """Find a hatchet in backpack OR equipped (hand slots)."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)

    # Check backpack
    if backpack:
        for it in world.items.values():
            if it.container == backpack and it.graphic in HATCHET_GRAPHICS:
                return it

    # Check equipped items (one_handed=0x01, two_handed=0x02)
    for layer in (0x01, 0x02):
        eq_serial = ss.equipment.get(layer)
        if eq_serial:
            it = world.items.get(eq_serial)
            if it and it.graphic in HATCHET_GRAPHICS:
                return it

    return None
