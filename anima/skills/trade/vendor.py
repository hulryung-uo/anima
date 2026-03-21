"""Vendor skills — buy from and sell to NPC merchants."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_buy_items,
    build_double_click,
    build_opl_request,
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


# Essential tool graphics — buy these when missing
HATCHET_GRAPHICS = {0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D}
SAW_GRAPHICS = {0x1034, 0x1035}
TINKER_TOOLS_GRAPHICS = {0x1EB8, 0x1EBC}
PICKAXE_GRAPHICS = {0x0E85, 0x0E86}

# Graphics to NEVER sell — essential tools and raw materials
KEEP_GRAPHICS: set[int] = (
    HATCHET_GRAPHICS | SAW_GRAPHICS | TINKER_TOOLS_GRAPHICS | PICKAXE_GRAPHICS
    | {0x1BDD, 0x1BD7}  # logs, boards
    | {0x19B7, 0x19B8, 0x19B9, 0x19BA}  # ore
    | {0x1BF2}  # ingots
    | {0x0EED}  # gold coins
    | {0x0E21}  # bandages
)

# (graphic, max_to_buy) — tools we always want to have
ESSENTIAL_TOOLS: list[tuple[int, int]] = [
    (0x0F43, 1),  # hatchet
    (0x1034, 1),  # saw
]


class BuyFromNpc(Skill):
    """Buy essential tools from an NPC vendor when they're missing."""

    name = "buy_from_npc"
    category = "trade"
    description = "Buy tools from an NPC vendor when missing essential tools."

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold < 10:
            return False
        if not _find_vendor(ctx):
            return False
        # Only buy if we're missing essential tools
        return bool(_find_missing_tools(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state
        vendor = await _find_vendor_async(ctx)
        if not vendor:
            return SkillResult(success=False, reward=-1.0, message="No vendor nearby")

        vendor_name = vendor.name or "vendor"
        missing = _find_missing_tools(ctx)

        from anima.core.publish import pub
        pub(ctx, "action.buy_start", f"Buying tools from {vendor_name}: {missing}")

        # Clear any stale vendor state
        ss.vendor_buy_list = []

        # Double-click vendor to open shop (triggers 0x3C + 0x74)
        await ctx.conn.send_packet(build_double_click(vendor.serial))
        logger.info("vendor_buy_opened", vendor=vendor_name, missing=missing)

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

        # Buy missing tools
        items_to_buy: list[tuple[int, int]] = []  # (serial, amount)
        total_cost = 0
        missing_graphics = {g for g, _ in missing}

        for bi in buy_list:
            if bi.graphic in missing_graphics:
                want = 1  # buy one of each missing tool
                cost = want * bi.price
                if total_cost + cost <= ss.gold:
                    items_to_buy.append((bi.serial, want))
                    total_cost += cost

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
        # Need sellable crafted goods in backpack and a vendor nearby
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        has_sellable = any(
            it.container == backpack and it.graphic not in KEEP_GRAPHICS
            for it in ctx.perception.world.items.values()
        )
        return has_sellable and bool(_find_vendor(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state
        vendor = await _find_vendor_async(ctx)
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

        # Sell items but protect essential tools and raw materials
        items_to_sell: list[tuple[int, int]] = [
            (si.serial, si.amount)
            for si in sell_list
            if si.graphic not in KEEP_GRAPHICS
        ]

        if not items_to_sell:
            ss.vendor_sell_list = []
            ss.vendor_serial = 0
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=0.0,
                message="Nothing worth selling",
                duration_ms=elapsed,
            )

        expected_gold = sum(
            si.price * si.amount for si in sell_list
            if si.graphic not in KEEP_GRAPHICS
        )

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


HUMAN_BODIES = {0x0190, 0x0191}  # male, female

# NPC title keywords that indicate a vendor who buys/sells
_VENDOR_TITLES = {
    "carpenter", "provisioner", "blacksmith", "tinker",
    "weaponsmith", "armorer", "bowyer", "tailor", "jeweler",
    "herbalist", "alchemist", "baker", "butcher", "cobbler",
    "furtrader", "tanner", "mage", "scribe", "shipwright",
    "innkeeper", "barkeep", "cook", "farmer", "fisherman",
    "vendor", "merchant", "shopkeeper",
}


def _is_vendor(mob: MobileInfo) -> bool:
    """Check if a mobile's OPL properties indicate it's a vendor."""
    if not mob.properties:
        return False
    # properties[0] = name, properties[1+] = title/attributes
    for prop in mob.properties:
        prop_lower = prop.lower()
        if any(t in prop_lower for t in _VENDOR_TITLES):
            return True
    return False


async def _find_vendor_async(ctx: "BrainContext") -> MobileInfo | None:
    """Find nearest vendor NPC, requesting OPL if needed.

    Priority:
    1. INVULNERABLE notoriety (standard ServUO)
    2. OPL properties contain vendor title
    3. Human NPC near known shop location (last resort)
    """
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=8)
    npcs = [
        m for m in nearby
        if m.serial != ss.serial and m.body in HUMAN_BODIES and m.serial < 0x10000
    ]
    npcs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

    if not npcs:
        return None

    # Pass 1: INVULNERABLE
    for m in npcs:
        if m.notoriety == NotorietyFlag.INVULNERABLE:
            return m

    # Pass 2: check OPL properties (request if missing)
    need_opl = [m for m in npcs if not m.properties]
    if need_opl:
        for m in need_opl[:5]:
            await ctx.conn.send_packet(build_opl_request(m.serial))
        await asyncio.sleep(0.5)  # wait for OPL responses

    for m in npcs:
        if _is_vendor(m):
            return m

    return None


def _find_vendor(ctx: "BrainContext") -> MobileInfo | None:
    """Synchronous vendor finder (uses cached properties only)."""
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=8)

    for m in sorted(nearby, key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y)):
        if m.serial == ss.serial:
            continue
        if m.notoriety == NotorietyFlag.INVULNERABLE:
            return m
        if m.body in HUMAN_BODIES and m.serial < 0x10000 and _is_vendor(m):
            return m

    return None


def _find_missing_tools(ctx: BrainContext) -> list[tuple[int, int]]:
    """Check which essential tools are missing from backpack + equipment.

    Returns list of (graphic, amount) that need to be purchased.
    """
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)

    # Collect all item graphics in backpack + equipment
    owned_graphics: set[int] = set()
    if backpack:
        for it in world.items.values():
            if it.container == backpack:
                owned_graphics.add(it.graphic)
    for layer in (0x01, 0x02):  # hand slots
        eq = ss.equipment.get(layer)
        if eq:
            it = world.items.get(eq)
            if it:
                owned_graphics.add(it.graphic)

    missing: list[tuple[int, int]] = []
    for graphic, amount in ESSENTIAL_TOOLS:
        if graphic not in owned_graphics:
            missing.append((graphic, amount))

    return missing
