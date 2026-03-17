"""Vendor skills — buy from and sell to NPC merchants."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click
from anima.perception.enums import NotorietyFlag
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()


class BuyFromNpc(Skill):
    """Open a vendor shop and browse items for purchase."""

    name = "buy_from_npc"
    category = "trade"
    description = "Open an NPC vendor shop. Requires gold and a vendor nearby."

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold <= 0:
            return False
        return bool(_find_vendor(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        vendor = _find_vendor(ctx)
        if not vendor:
            return SkillResult(success=False, reward=-1.0, message="No vendor nearby")

        vendor_name = vendor.name or "vendor"

        # Double-click vendor to open shop
        await ctx.conn.send_packet(build_double_click(vendor.serial))
        logger.info("vendor_opened", vendor=vendor_name)

        # Wait for shop gump to arrive
        await asyncio.sleep(1.0)

        elapsed = (time.monotonic() - start) * 1000

        # NOTE: Actual buying requires parsing the shop gump (0x24/0x74)
        # and sending build_buy_items(). For now, just opening the shop
        # is the action — purchase logic will be added when gump handling
        # is implemented.

        return SkillResult(
            success=True,
            reward=1.0,
            message=f"Opened shop: {vendor_name}",
            duration_ms=elapsed,
        )


class SellToNpc(Skill):
    """Sell items to an NPC vendor."""

    name = "sell_to_npc"
    category = "trade"
    description = "Sell items from your backpack to a nearby NPC vendor."

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        # Need items in backpack and a vendor nearby
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        has_items = any(
            it.container == backpack
            for it in ctx.perception.world.items.values()
        )
        return has_items and bool(_find_vendor(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        vendor = _find_vendor(ctx)
        if not vendor:
            return SkillResult(success=False, reward=-1.0, message="No vendor nearby")

        vendor_name = vendor.name or "vendor"
        gold_before = ctx.perception.self_state.gold

        # Double-click vendor to open sell interface
        await ctx.conn.send_packet(build_double_click(vendor.serial))
        await asyncio.sleep(1.0)

        # NOTE: Actual selling requires parsing sell gump and sending
        # build_sell_items(). For now this opens the vendor interface.

        gold_after = ctx.perception.self_state.gold
        gold_earned = max(0, gold_after - gold_before)
        elapsed = (time.monotonic() - start) * 1000

        reward = 1.0 + gold_earned * 0.1 if gold_earned > 0 else 0.5
        return SkillResult(
            success=True,
            reward=reward,
            message=f"Opened sell to {vendor_name}" + (
                f", earned {gold_earned}gp" if gold_earned else ""
            ),
            duration_ms=elapsed,
        )


def _find_vendor(ctx: BrainContext):
    """Find nearest NPC vendor (invulnerable notoriety)."""
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=5)
    vendors = [
        m for m in nearby
        if m.notoriety == NotorietyFlag.INVULNERABLE
    ]
    if not vendors:
        return None
    vendors.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return vendors[0]
