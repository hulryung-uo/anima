"""Mining skill — use a pickaxe on rocks to gather ore."""

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

PICKAXE_GRAPHICS = {0x0E86, 0x0E85}  # Pickaxe variants
SHOVEL_GRAPHIC = 0x0F39

# Rock/mountain tile IDs that can be mined
MINEABLE_GRAPHICS = {
    0x08B0, 0x08B1, 0x08B2, 0x08B3, 0x08B4, 0x08B5, 0x08B6, 0x08B7,
    0x08B8, 0x08B9, 0x08BA, 0x08BB, 0x08BC, 0x08BD, 0x08BE, 0x08BF,
    0x08C0, 0x08C1, 0x08C2, 0x08C3, 0x08C4, 0x08C5, 0x08C6, 0x08C7,
    # Cave tiles
    0x0555, 0x0556, 0x0557, 0x0558, 0x0559, 0x055A,
}

ORE_GRAPHICS = {0x19B7, 0x19B8, 0x19B9, 0x19BA}
MINING_SKILL_ID = 45


class MineOre(Skill):
    """Mine rocks with a pickaxe to gather ore."""

    name = "mine_ore"
    category = "gathering"
    description = "Use a pickaxe on nearby rocks to mine ore. Requires pickaxe and rocks nearby."
    required_items = list(PICKAXE_GRAPHICS)
    required_nearby = list(MINEABLE_GRAPHICS)
    required_skill = (MINING_SKILL_ID, 0.0)  # Any mining skill level

    async def can_execute(self, ctx: BrainContext) -> bool:
        """Check if we have a pickaxe AND rocks nearby."""
        ss = ctx.perception.self_state
        world = ctx.perception.world

        # Check for any pickaxe/shovel in backpack
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        has_tool = any(
            it.graphic in PICKAXE_GRAPHICS or it.graphic == SHOVEL_GRAPHIC
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_tool:
            return False

        # Check for nearby mineable tiles
        nearby = world.nearby_items(ss.x, ss.y, distance=3)
        has_rock = any(it.graphic in MINEABLE_GRAPHICS for it in nearby)
        return has_rock

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        # Find pickaxe
        backpack = ss.equipment.get(0x15)
        tool = None
        for item in world.items.values():
            if item.container == backpack and (
                item.graphic in PICKAXE_GRAPHICS
                or item.graphic == SHOVEL_GRAPHIC
            ):
                tool = item
                break

        if not tool:
            return SkillResult(success=False, reward=-1.0, message="No mining tool")

        # Find nearest rock
        nearby = world.nearby_items(ss.x, ss.y, distance=3)
        rock = None
        for item in nearby:
            if item.graphic in MINEABLE_GRAPHICS:
                rock = item
                break

        if not rock:
            return SkillResult(success=False, reward=-1.0, message="No rocks nearby")

        # Count ore before
        ore_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )

        # Double-click tool
        await ctx.conn.send_packet(build_double_click(tool.serial))
        await asyncio.sleep(0.5)

        # Target the rock
        await ctx.conn.send_packet(build_target_response(
            target_type=1,  # ground/tile target
            cursor_id=0,
            x=rock.x,
            y=rock.y,
            z=rock.z,
            graphic=rock.graphic,
        ))

        # Wait for mining animation + result
        await asyncio.sleep(3.0)

        # Count ore after
        ore_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )

        elapsed = (time.monotonic() - start) * 1000
        ore_gained = ore_after - ore_before

        if ore_gained > 0:
            reward = 5.0 + ore_gained
            logger.info("mine_success", ore=ore_gained, duration_ms=f"{elapsed:.0f}")
            return SkillResult(
                success=True,
                reward=reward,
                message=f"Mined {ore_gained} ore",
                skill_gains=[(MINING_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        else:
            logger.info("mine_fail", duration_ms=f"{elapsed:.0f}")
            return SkillResult(
                success=False,
                reward=-1.0,
                message="No ore found",
                duration_ms=elapsed,
            )
