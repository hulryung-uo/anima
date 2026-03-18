"""Vendor skills — buy from and sell to NPC merchants."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_buy_items,
    build_double_click,
    build_sell_items,
    build_unicode_speech,
)
from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Max time (seconds) to wait for vendor list packets from the server
_VENDOR_LIST_TIMEOUT = 3.0
# Poll interval while waiting for vendor list
_POLL_INTERVAL = 0.2


async def _wait_for_sell_list(ctx: BrainContext, timeout: float = _VENDOR_LIST_TIMEOUT) -> bool:
    """Poll until vendor_sell_list is populated or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.perception.self_state.vendor_sell_list:
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return False


async def _wait_for_buy_list(ctx: BrainContext, timeout: float = _VENDOR_LIST_TIMEOUT) -> bool:
    """Poll until vendor_buy_list is populated or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.perception.self_state.vendor_buy_list:
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return False


class BuyFromNpc(Skill):
    """Buy specific items from an NPC vendor."""

    name = "buy_from_npc"
    category = "trade"
    description = "Buy items from an NPC vendor. Requires gold and a vendor nearby."

    # Subclasses or config can set desired items to buy: list of (graphic, max_amount)
    # If empty, buys nothing (just opens the shop).
    buy_targets: list[tuple[int, int]] = []

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold <= 0:
            return False
        return bool(_find_vendor(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state
        vendor = _find_vendor(ctx)
        if not vendor:
            return SkillResult(success=False, reward=-1.0, message="No vendor nearby")

        vendor_name = vendor.name or "vendor"

        # Clear any stale vendor state
        ss.vendor_buy_list = []

        # Double-click vendor to open shop (triggers 0x3C + 0x74)
        await ctx.conn.send_packet(build_double_click(vendor.serial))
        logger.info("vendor_buy_opened", vendor=vendor_name)

        # Wait for buy list to arrive
        got_list = await _wait_for_buy_list(ctx)
        if not got_list:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False,
                reward=-0.5,
                message=f"No buy list from {vendor_name}",
                duration_ms=elapsed,
            )

        buy_list = ss.vendor_buy_list
        logger.info("vendor_buy_list_received", count=len(buy_list))

        # Determine what to buy
        items_to_buy: list[tuple[int, int]] = []  # (serial, amount)
        total_cost = 0

        if self.buy_targets:
            # Buy specific items by graphic
            target_map = {g: amt for g, amt in self.buy_targets}
            for bi in buy_list:
                if bi.graphic in target_map:
                    want = min(bi.amount, target_map[bi.graphic])
                    cost = want * bi.price
                    if total_cost + cost <= ss.gold:
                        items_to_buy.append((bi.serial, want))
                        total_cost += cost
        else:
            # No specific targets — just report what's available
            logger.info(
                "vendor_buy_items_available",
                items=[(bi.name, bi.price, bi.amount) for bi in buy_list[:10]],
            )

        gold_before = ss.gold

        if items_to_buy:
            await ctx.conn.send_packet(build_buy_items(ss.vendor_serial, items_to_buy))
            logger.info(
                "vendor_buy_sent",
                items=len(items_to_buy),
                cost=total_cost,
            )
            # Wait for server to process the transaction
            await asyncio.sleep(0.5)

        # Clear vendor state
        ss.vendor_buy_list = []
        ss.vendor_serial = 0

        gold_after = ss.gold
        gold_spent = max(0, gold_before - gold_after)
        elapsed = (time.monotonic() - start) * 1000

        if items_to_buy:
            reward = 1.0 + gold_spent * 0.01
            message = f"Bought {len(items_to_buy)} item(s) from {vendor_name} for {gold_spent}gp"
        else:
            reward = 0.5
            message = f"Browsed shop: {vendor_name} ({len(buy_list)} items)"

        return SkillResult(
            success=True,
            reward=reward,
            message=message,
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
        has_items = any(it.container == backpack for it in ctx.perception.world.items.values())
        return has_items and bool(_find_vendor(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state
        vendor = _find_vendor(ctx)
        if not vendor:
            return SkillResult(success=False, reward=-1.0, message="No vendor nearby")

        vendor_name = vendor.name or "vendor"
        gold_before = ss.gold

        # Clear any stale vendor state
        ss.vendor_sell_list = []

        # Say "vendor sell" to trigger the sell list (0x9E)
        await ctx.conn.send_packet(build_unicode_speech("vendor sell"))
        logger.info("vendor_sell_requested", vendor=vendor_name)

        # Wait for sell list to arrive
        got_list = await _wait_for_sell_list(ctx)
        if not got_list:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False,
                reward=-0.5,
                message=f"No sell list from {vendor_name}",
                duration_ms=elapsed,
            )

        sell_list = ss.vendor_sell_list
        logger.info("vendor_sell_list_received", count=len(sell_list))

        if not sell_list:
            ss.vendor_serial = 0
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False,
                reward=-0.5,
                message=f"{vendor_name} won't buy anything",
                duration_ms=elapsed,
            )

        # Sell ALL items from the sell list
        items_to_sell: list[tuple[int, int]] = [(si.serial, si.amount) for si in sell_list]
        expected_gold = sum(si.price * si.amount for si in sell_list)

        await ctx.conn.send_packet(build_sell_items(ss.vendor_serial, items_to_sell))
        logger.info(
            "vendor_sell_sent",
            items=len(items_to_sell),
            expected_gold=expected_gold,
        )

        # Wait for server to process the transaction
        await asyncio.sleep(0.5)

        # Clear vendor state
        ss.vendor_sell_list = []
        ss.vendor_serial = 0

        gold_after = ss.gold
        gold_earned = max(0, gold_after - gold_before)
        elapsed = (time.monotonic() - start) * 1000

        reward = 1.0 + gold_earned * 0.1 if gold_earned > 0 else 0.5
        return SkillResult(
            success=True,
            reward=reward,
            message=f"Sold {len(items_to_sell)} item(s) to {vendor_name}"
            + (f", earned {gold_earned}gp" if gold_earned else f" (expected ~{expected_gold}gp)"),
            duration_ms=elapsed,
        )


def _find_vendor(ctx: BrainContext) -> MobileInfo | None:
    """Find nearest NPC vendor (invulnerable notoriety)."""
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=5)
    vendors = [m for m in nearby if m.notoriety == NotorietyFlag.INVULNERABLE]
    if not vendors:
        return None
    vendors.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return vendors[0]
