"""Smelting skill — convert ore into ingots at a forge."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

ORE_GRAPHICS = {0x19B7, 0x19B8, 0x19B9, 0x19BA}
INGOT_GRAPHICS = {0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1}
FORGE_GRAPHICS = {0x0FB1, 0x197A, 0x197E, 0x19A9, 0x0DE3, 0x0DE6}
MINING_SKILL_ID = 45


class SmeltOre(Skill):
    """Smelt ore into ingots at a nearby forge."""

    name = "smelt_ore"
    category = "crafting"
    description = "Convert ore into metal ingots using a forge. Requires ore and a forge nearby."
    required_skill = (MINING_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        has_ore = any(
            it.graphic in ORE_GRAPHICS
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_ore:
            return False

        nearby = world.nearby_items(ss.x, ss.y, distance=3)
        return any(it.graphic in FORGE_GRAPHICS for it in nearby)

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        backpack = ss.equipment.get(0x15)

        # Find ore in backpack
        ore = None
        for item in world.items.values():
            if item.container == backpack and item.graphic in ORE_GRAPHICS:
                ore = item
                break

        if not ore:
            return SkillResult(success=False, reward=-1.0, message="No ore")

        # Count ingots before
        ingots_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        # Double-click ore — server auto-detects nearby forge
        await ctx.conn.send_packet(build_double_click(ore.serial))

        # Wait for smelting result
        await asyncio.sleep(2.0)

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        elapsed = (time.monotonic() - start) * 1000
        ingots_gained = ingots_after - ingots_before

        if ingots_gained > 0:
            reward = 5.0 + ingots_gained * 0.5
            logger.info("smelt_success", ingots=ingots_gained)
            return SkillResult(
                success=True, reward=reward,
                message=f"Smelted {ingots_gained} ingots",
                skill_gains=[(MINING_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        else:
            return SkillResult(
                success=False, reward=-2.0,
                message="Smelting failed",
                items_consumed=[ore.serial],
                duration_ms=elapsed,
            )
