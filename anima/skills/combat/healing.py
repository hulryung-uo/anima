"""Healing skill — use bandages to heal self."""

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

BANDAGE_GRAPHIC = 0x0E21


class HealSelf(Skill):
    """Use bandages to heal yourself."""

    name = "heal_self"
    category = "combat"
    description = "Use bandages to restore HP. Requires bandages in backpack."
    required_items = [BANDAGE_GRAPHIC]

    async def can_execute(self, ctx: BrainContext) -> bool:
        if not await super().can_execute(ctx):
            return False
        # Only heal if wounded
        return ctx.perception.self_state.hp_percent < 90.0

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        start = time.monotonic()
        hp_before = ss.hits

        # Find bandage in backpack
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return SkillResult(success=False, reward=-1.0, message="No backpack")

        bandage = None
        for item in ctx.perception.world.items.values():
            if item.container == backpack and item.graphic == BANDAGE_GRAPHIC:
                bandage = item
                break

        if not bandage:
            return SkillResult(success=False, reward=-1.0, message="No bandages")

        # Double-click bandage
        await ctx.conn.send_packet(build_double_click(bandage.serial))

        # Wait briefly for target cursor
        await asyncio.sleep(0.5)

        # Target self
        await ctx.conn.send_packet(build_target_response(
            target_type=0,
            cursor_id=0,
            serial=ss.serial,
        ))

        # Wait for healing to complete (~5 seconds for bandage)
        await asyncio.sleep(5.0)

        hp_after = ss.hits
        healed = max(0, hp_after - hp_before)
        elapsed = (time.monotonic() - start) * 1000

        if healed > 0:
            reward = 1.0 + healed * 0.2
            logger.info("heal_self_success", healed=healed, hp=f"{hp_after}/{ss.hits_max}")
            return SkillResult(
                success=True,
                reward=reward,
                message=f"Healed {healed} HP",
                items_consumed=[bandage.serial],
                duration_ms=elapsed,
            )
        else:
            logger.info("heal_self_no_effect", hp=f"{hp_after}/{ss.hits_max}")
            return SkillResult(
                success=False,
                reward=-0.5,
                message="Healing had no effect",
                items_consumed=[bandage.serial],
                duration_ms=elapsed,
            )
