"""Vendor skills — buy from and sell to NPC merchants via context menu."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_buy_items,
    build_context_menu_request,
    build_context_menu_selection,
    build_opl_request,
    build_sell_items,
)
from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Context menu cliloc IDs (from ServUO BaseVendor.cs)
# Generic clilocs use the 3006xxx range (base 3000000 + index)
_CLILOC_VENDOR_BUY = 3006103
_CLILOC_VENDOR_SELL = 3006104

# Max time (seconds) to wait for vendor list packets from the server
_VENDOR_LIST_TIMEOUT = 3.0
# Context menu should respond almost instantly
_CONTEXT_MENU_TIMEOUT = 1.5
# Poll interval while waiting
_POLL_INTERVAL = 0.2
# Per-vendor cooldown after refusing to buy (seconds)
_VENDOR_REFUSE_COOLDOWN = 300.0  # 5 min — allow retry after transient failures
# Global cooldown after buy failure due to no gold (seconds)
_BUY_NO_GOLD_COOLDOWN = 600.0


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


async def _wait_for_context_menu(
    ctx: BrainContext, timeout: float = _CONTEXT_MENU_TIMEOUT,
) -> bool:
    """Poll until context_menu is populated or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.perception.self_state.context_menu:
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return False


async def _request_context_menu_entry(
    ctx: BrainContext, vendor: MobileInfo, cliloc: int,
) -> bool:
    """Request context menu from vendor and select the entry matching cliloc.

    Returns True if the entry was found and selected.
    """
    ss = ctx.perception.self_state
    ss.context_menu = []
    ss.context_menu_serial = 0

    await ctx.conn.send_packet(build_context_menu_request(vendor.serial))

    if not await _wait_for_context_menu(ctx):
        logger.warning("context_menu_timeout", vendor=vendor.name)
        return False

    # Find the entry with matching cliloc
    for entry in ss.context_menu:
        if entry.cliloc == cliloc:
            if entry.flags == 0:  # enabled
                await ctx.conn.send_packet(
                    build_context_menu_selection(vendor.serial, entry.index)
                )
                logger.info(
                    "context_menu_selected",
                    vendor=vendor.name,
                    cliloc=cliloc,
                    index=entry.index,
                )
                ss.context_menu = []
                return True
            else:
                # Entry exists but disabled — walk closer and retry
                logger.info(
                    "context_menu_disabled",
                    vendor=vendor.name,
                    cliloc=cliloc,
                    flags=entry.flags,
                )
                ss.context_menu = []
                from anima.action.movement import go_to
                await go_to(ctx, vendor.x, vendor.y)
                # Retry once after walking closer
                ss.context_menu = []
                ss.context_menu_serial = 0
                await ctx.conn.send_packet(
                    build_context_menu_request(vendor.serial)
                )
                if not await _wait_for_context_menu(ctx):
                    return False
                for retry_entry in ss.context_menu:
                    if retry_entry.cliloc == cliloc and retry_entry.flags == 0:
                        await ctx.conn.send_packet(
                            build_context_menu_selection(
                                vendor.serial, retry_entry.index,
                            )
                        )
                        logger.info(
                            "context_menu_selected_after_walk",
                            vendor=vendor.name,
                            cliloc=cliloc,
                        )
                        ss.context_menu = []
                        return True
                ss.context_menu = []
                return False

    logger.warning(
        "context_menu_entry_not_found",
        vendor=vendor.name,
        cliloc=cliloc,
        available=[(e.cliloc, e.index, e.flags) for e in ss.context_menu],
    )
    ss.context_menu = []
    return False


# Essential tool graphics — buy these when missing
HATCHET_GRAPHICS = {0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D}
SAW_GRAPHICS = {0x1034, 0x1035}
TINKER_TOOLS_GRAPHICS = {0x1EB8, 0x1EBC}
PICKAXE_GRAPHICS = {0x0E85, 0x0E86}
SMITH_HAMMER_GRAPHICS = {0x13E3, 0x13E4}
TONGS_GRAPHICS = {0x0FBB, 0x0FBC}

# Graphics to NEVER sell — essential tools and raw materials
KEEP_GRAPHICS: set[int] = (
    HATCHET_GRAPHICS | SAW_GRAPHICS | TINKER_TOOLS_GRAPHICS | PICKAXE_GRAPHICS
    | SMITH_HAMMER_GRAPHICS | TONGS_GRAPHICS
    | {0x1BDD, 0x1BD7}  # logs, boards
    | {0x19B7, 0x19B8, 0x19B9, 0x19BA}  # ore
    | {0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1}  # ingots (all stack-size graphics)
    | {0x0EED}  # gold coins
    | {0x0E21}  # bandages
)

# (graphic, max_to_buy) — tools we always want to have
ESSENTIAL_TOOLS: list[tuple[int, int]] = [
    (0x0F43, 1),  # hatchet
    (0x1034, 1),  # saw
    (0x0E86, 3),  # pickaxe — buy 3
    (0x13E3, 1),  # smith hammer
]


class BuyFromNpc(Skill):
    """Buy essential tools from an NPC vendor via context menu."""

    name = "buy_from_npc"
    category = "trade"
    description = "Buy tools from an NPC vendor when missing essential tools."

    async def can_execute(self, ctx: BrainContext) -> bool:
        # Global cooldown — don't retry buying if we recently failed (no gold)
        buy_cooldown_until = ctx.blackboard.get("buy_cooldown_until", 0.0)
        if time.monotonic() < buy_cooldown_until:
            return False
        missing = _find_missing_tools(ctx)
        if not missing:
            return False
        vendor = _find_vendor(ctx)
        if not vendor:
            return False
        logger.debug(
            "buy_can_execute",
            missing=[f"0x{g:04X}" for g, _ in missing],
            vendor=vendor.name,
        )
        return True

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state
        vendor = await _find_vendor_async(ctx)
        if not vendor:
            return SkillResult(
                success=False, reward=-1.0,
                message="No vendor found nearby",
            )

        vendor_name = vendor.name or "vendor"
        logger.info(
            "vendor_buy_attempt",
            vendor=vendor_name,
            serial=f"0x{vendor.serial:08X}",
            vendor_pos=f"({vendor.x},{vendor.y})",
            player_pos=f"({ss.x},{ss.y})",
            dist=max(abs(vendor.x - ss.x), abs(vendor.y - ss.y)),
        )

        # Walk closer if vendor is far
        dist = max(abs(vendor.x - ss.x), abs(vendor.y - ss.y))
        if dist > 2:
            from anima.action.movement import go_to
            logger.info("vendor_walking_closer", vendor=vendor_name, dist=dist)
            await go_to(ctx, vendor.x, vendor.y)

        missing = _find_missing_tools(ctx)

        from anima.core.publish import pub
        pub(ctx, "action.buy_start", f"Buying tools from {vendor_name}: {missing}")

        # Clear stale state
        ss.vendor_buy_list = []

        # Open buy via context menu
        if not await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_BUY):
            _mark_refused(ctx, vendor.serial)
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-2.0,
                message=f"No Buy option on {vendor_name} — skipping",
                duration_ms=elapsed,
            )

        # Wait for buy list to arrive
        got_list = await _wait_for_buy_list(ctx)
        if not got_list:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-0.5,
                message=f"No buy list from {vendor_name}",
                duration_ms=elapsed,
            )

        buy_list = ss.vendor_buy_list

        # Log all items in vendor's buy list
        buy_list_detail = [
            f"{bi.name or f'0x{bi.graphic:04X}'} x{bi.amount} @{bi.price}gp"
            for bi in buy_list
        ]
        logger.info(
            "vendor_buy_list_received",
            vendor=vendor_name,
            count=len(buy_list),
            items=buy_list_detail,
        )

        # Buy missing tools
        items_to_buy: list[tuple[int, int]] = []  # (serial, amount)
        total_cost = 0
        missing_want = {g: amt for g, amt in missing}
        buying_detail: list[str] = []

        for bi in buy_list:
            if bi.graphic in missing_want:
                want = missing_want[bi.graphic]
                items_to_buy.append((bi.serial, want))
                total_cost += want * bi.price
                buying_detail.append(
                    f"{bi.name or f'0x{bi.graphic:04X}'} x{want} @{bi.price}gp"
                )

        gold_before = ss.gold

        if not items_to_buy:
            # Vendor doesn't sell what we need — mark refused so we don't loop
            ss.vendor_buy_list = []
            ss.vendor_serial = 0
            _mark_refused(ctx, vendor.serial)
            elapsed = (time.monotonic() - start) * 1000
            missing_names = [f"0x{g:04X}" for g in missing_want]
            logger.warning(
                "vendor_buy_no_match",
                vendor=vendor_name,
                needed=missing_names,
                available=[f"0x{bi.graphic:04X}" for bi in buy_list],
                reason="vendor doesn't sell the tools we need",
            )
            return SkillResult(
                success=False, reward=-1.0,
                message=f"{vendor_name} doesn't sell needed tools "
                        f"(need: {', '.join(missing_names)})",
                duration_ms=elapsed,
            )

        missing_before = set(missing_want.keys())

        logger.info(
            "vendor_buy_sending",
            vendor=vendor_name,
            buying=buying_detail,
            total_cost=total_cost,
            gold_before=gold_before,
        )

        await ctx.conn.send_packet(build_buy_items(ss.vendor_serial, items_to_buy))
        await asyncio.sleep(0.5)

        # Clear vendor state
        ss.vendor_buy_list = []
        ss.vendor_serial = 0

        gold_after = ss.gold
        gold_spent = max(0, gold_before - gold_after)

        # Verify purchase: check if missing tools actually arrived
        still_missing = _find_missing_tools(ctx)
        missing_after = {g for g, _ in still_missing}
        items_received = len(missing_before - missing_after)

        elapsed = (time.monotonic() - start) * 1000

        logger.info(
            "vendor_buy_result",
            vendor=vendor_name,
            items_requested=len(items_to_buy),
            items_received=items_received,
            gold_before=gold_before,
            gold_after=gold_after,
            gold_spent=gold_spent,
            total_cost=total_cost,
            still_missing=[f"0x{g:04X}" for g in missing_after] if missing_after else None,
        )

        if items_received == 0 and gold_spent == 0:
            # Nothing changed — purchase failed (likely no gold)
            # Set global buy cooldown so we stop trying ALL vendors
            ctx.blackboard["buy_cooldown_until"] = (
                time.monotonic() + _BUY_NO_GOLD_COOLDOWN
            )
            # Trigger rethink so LLM picks a different plan
            ctx.blackboard["skill_problem"] = (
                f"Cannot buy tools — no gold (have {gold_after}gp). "
                "Need to earn gold first: mine ore, chop wood, craft items and sell."
            )
            ctx.blackboard["last_think_time"] = 0.0  # force immediate rethink
            logger.warning(
                "vendor_buy_failed",
                vendor=vendor_name,
                buying=buying_detail,
                reason="no items received, no gold spent",
                gold=gold_after,
                cooldown_s=_BUY_NO_GOLD_COOLDOWN,
            )
            return SkillResult(
                success=False, reward=-2.0,
                message=f"Buy from {vendor_name} failed — no gold? "
                        f"(have {gold_after}gp, wanted {', '.join(buying_detail)})",
                duration_ms=elapsed,
            )

        reward = 1.0 + gold_spent * 0.01
        message = (
            f"Bought {items_received} item(s) from {vendor_name} for {gold_spent}gp "
            f"({', '.join(buying_detail)})"
        )

        return SkillResult(
            success=True, reward=reward, message=message, duration_ms=elapsed,
        )


class SellToNpc(Skill):
    """Sell items to an NPC vendor via context menu."""

    name = "sell_to_npc"
    category = "trade"
    description = "Sell items from your backpack to a nearby NPC vendor."

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        # Only sell when carrying enough weight (>= 60%)
        if ss.weight_max > 0 and ss.weight / ss.weight_max < 0.6:
            return False
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        # Need sellable items in backpack
        has_sellable = any(
            it.container == backpack and it.graphic not in KEEP_GRAPHICS
            for it in ctx.perception.world.items.values()
        )
        if not has_sellable:
            return False
        # Need a non-refused vendor actually detected nearby
        vendor = _find_vendor(ctx)
        return vendor is not None and not _is_refused(ctx, vendor.serial)

    async def execute(self, ctx: BrainContext) -> SkillResult:
        start = time.monotonic()
        ss = ctx.perception.self_state

        vendor = await _find_vendor_async(ctx)
        if not vendor:
            return SkillResult(
                success=False, reward=-1.0,
                message="No vendor found nearby",
            )

        vendor_name = vendor.name or "vendor"
        logger.info(
            "vendor_sell_attempt",
            vendor=vendor_name,
            serial=f"0x{vendor.serial:08X}",
            vendor_pos=f"({vendor.x},{vendor.y})",
            player_pos=f"({ss.x},{ss.y})",
            dist=max(abs(vendor.x - ss.x), abs(vendor.y - ss.y)),
        )

        # Walk closer if vendor is far
        dist = max(abs(vendor.x - ss.x), abs(vendor.y - ss.y))
        if dist > 2:
            from anima.action.movement import go_to
            logger.info("vendor_walking_closer", vendor=vendor_name, dist=dist)
            await go_to(ctx, vendor.x, vendor.y)

        gold_before = ss.gold

        # Clear stale state
        ss.vendor_sell_list = []

        # Open sell via context menu
        if not await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_SELL):
            _mark_refused(ctx, vendor.serial)
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-2.0,
                message=f"No Sell option on {vendor_name} — skipping",
                duration_ms=elapsed,
            )

        # Wait for sell list to arrive
        got_list = await _wait_for_sell_list(ctx)
        if not got_list:
            _mark_refused(ctx, vendor.serial)
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-2.0,
                message=f"No sell list from {vendor_name} — skipping",
                duration_ms=elapsed,
            )

        sell_list = ss.vendor_sell_list

        # Log everything the vendor is willing to buy
        sell_list_detail = [
            f"{si.name or f'0x{si.graphic:04X}'} x{si.amount} @{si.price}gp"
            + (" [KEEP]" if si.graphic in KEEP_GRAPHICS else "")
            for si in sell_list
        ]
        logger.info(
            "vendor_sell_list_received",
            vendor=vendor_name,
            count=len(sell_list),
            items=sell_list_detail,
        )

        if not sell_list:
            ss.vendor_serial = 0
            _mark_refused(ctx, vendor.serial)
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "vendor_sell_empty_list",
                vendor=vendor_name,
                reason="vendor returned empty sell list",
            )
            return SkillResult(
                success=False, reward=-2.0,
                message=f"{vendor_name} won't buy anything — empty sell list",
                duration_ms=elapsed,
            )

        # Dynamic keep set — allow selling ingots when crafting is impossible
        from anima.actions.inventory import find_in_backpack

        keep = set(KEEP_GRAPHICS)
        if not find_in_backpack(ctx, TONGS_GRAPHICS):
            for _ig in (0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1):
                keep.discard(_ig)  # ingots sellable without tongs

        # Sell items but protect essential tools and raw materials
        items_to_sell: list[tuple[int, int]] = [
            (si.serial, si.amount)
            for si in sell_list
            if si.graphic not in keep
        ]
        selling_detail = [
            f"{si.name or f'0x{si.graphic:04X}'} x{si.amount} @{si.price}gp"
            for si in sell_list
            if si.graphic not in keep
        ]
        kept_items = [
            f"{si.name or f'0x{si.graphic:04X}'}"
            for si in sell_list
            if si.graphic in keep
        ]

        if not items_to_sell:
            ss.vendor_sell_list = []
            ss.vendor_serial = 0
            elapsed = (time.monotonic() - start) * 1000
            logger.info(
                "vendor_sell_all_protected",
                vendor=vendor_name,
                kept=kept_items,
                reason="all items in sell list are KEEP_GRAPHICS protected",
            )
            return SkillResult(
                success=False, reward=0.0,
                message=f"Nothing to sell to {vendor_name} "
                        f"— all protected ({', '.join(kept_items[:3])})",
                duration_ms=elapsed,
            )

        expected_gold = sum(
            si.price * si.amount for si in sell_list
            if si.graphic not in keep
        )

        logger.info(
            "vendor_sell_sending",
            vendor=vendor_name,
            selling=selling_detail,
            keeping=kept_items if kept_items else None,
            expected_gold=expected_gold,
            gold_before=gold_before,
        )

        await ctx.conn.send_packet(build_sell_items(ss.vendor_serial, items_to_sell))

        await asyncio.sleep(0.5)

        # Clear vendor state
        ss.vendor_sell_list = []
        ss.vendor_serial = 0

        gold_after = ss.gold
        gold_earned = max(0, gold_after - gold_before)
        elapsed = (time.monotonic() - start) * 1000

        logger.info(
            "vendor_sell_result",
            vendor=vendor_name,
            items_sold=len(items_to_sell),
            gold_before=gold_before,
            gold_after=gold_after,
            gold_earned=gold_earned,
            expected_gold=expected_gold,
            sold=selling_detail,
        )

        if gold_earned == 0 and expected_gold > 0:
            logger.warning(
                "vendor_sell_no_gold",
                vendor=vendor_name,
                reason="sell packet sent but gold did not change — may have failed",
                expected=expected_gold,
                sold=selling_detail,
            )

        reward = 1.0 + gold_earned * 0.1 if gold_earned > 0 else 0.5
        msg = f"Sold {len(items_to_sell)} item(s) to {vendor_name}"
        if gold_earned > 0:
            msg += f", earned {gold_earned}gp ({', '.join(selling_detail)})"
        elif expected_gold > 0:
            msg += (f" (expected ~{expected_gold}gp but got 0 — "
                    f"may have failed: {', '.join(selling_detail)})")
        else:
            msg += f" ({', '.join(selling_detail)})"

        return SkillResult(
            success=True,
            reward=reward,
            message=msg,
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

# NPCs with these titles look like vendors but don't sell items
_NON_VENDOR_TITLES = {
    "guildmaster", "guildmistress", "guild master", "guild mistress",
}


def _is_vendor(mob: MobileInfo) -> bool:
    """Check if a mobile is a vendor by name or OPL properties."""
    name_lower = (mob.name or "").lower()
    # Exclude guildmasters — they don't sell items
    if any(t in name_lower for t in _NON_VENDOR_TITLES):
        return False
    if any(t in name_lower for t in _VENDOR_TITLES):
        return True
    for prop in (mob.properties or []):
        prop_lower = prop.lower()
        if any(t in prop_lower for t in _NON_VENDOR_TITLES):
            return False
        if any(t in prop_lower for t in _VENDOR_TITLES):
            return True
    return False


_VENDOR_RANGE = 8


def _mark_refused(ctx: "BrainContext", serial: int) -> None:
    """Mark a vendor as refused — won't retry for _VENDOR_REFUSE_COOLDOWN."""
    refused: dict[int, float] = ctx.blackboard.setdefault("refused_vendors", {})
    refused[serial] = time.monotonic()


def _is_refused(ctx: "BrainContext", serial: int) -> bool:
    """Check if a vendor was recently refused."""
    refused: dict[int, float] = ctx.blackboard.get("refused_vendors", {})
    ts = refused.get(serial, 0.0)
    if time.monotonic() - ts < _VENDOR_REFUSE_COOLDOWN:
        return True
    # Expired — clean up
    refused.pop(serial, None)
    return False


async def _find_vendor_async(ctx: "BrainContext") -> MobileInfo | None:
    """Find nearest non-refused vendor NPC, requesting OPL if needed."""
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(
        ss.x, ss.y, distance=_VENDOR_RANGE,
    )
    npcs = [
        m for m in nearby
        if m.serial != ss.serial
        and m.body in HUMAN_BODIES
        and m.serial < 0x10000
        and not _is_refused(ctx, m.serial)
    ]
    npcs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

    if not npcs:
        return None

    for m in npcs:
        if m.notoriety == NotorietyFlag.INVULNERABLE and _is_vendor(m):
            return m

    for m in npcs:
        if m.name and _is_vendor(m):
            return m

    need_opl = [m for m in npcs if not m.properties]
    if need_opl:
        for m in need_opl[:5]:
            await ctx.conn.send_packet(build_opl_request(m.serial))
        await asyncio.sleep(0.5)

    for m in npcs:
        if _is_vendor(m):
            return m

    return None


def _find_vendor(
    ctx: "BrainContext",
    vendor_types: set[str] | None = None,
    check_refused: bool = True,
) -> MobileInfo | None:
    """Find non-refused vendor within range (sync, for can_execute).

    vendor_types: if given, only match vendors whose name/properties contain
    one of these keywords (e.g. {"blacksmith", "weaponsmith"}).
    check_refused: if False, skip the refused-vendor check (useful for buy
    operations — a vendor that refused a sell may still accept buys).
    """
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(
        ss.x, ss.y, distance=_VENDOR_RANGE,
    )

    def _matches_type(mob: MobileInfo) -> bool:
        if not vendor_types:
            return True
        name_lower = (mob.name or "").lower()
        if any(vt in name_lower for vt in vendor_types):
            return True
        for prop in (mob.properties or []):
            if any(vt in prop.lower() for vt in vendor_types):
                return True
        return False

    for m in sorted(nearby, key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y)):
        if m.serial == ss.serial:
            continue
        if check_refused and _is_refused(ctx, m.serial):
            continue
        if m.body not in HUMAN_BODIES or m.serial >= 0x10000:
            continue
        if m.notoriety == NotorietyFlag.INVULNERABLE and _is_vendor(m) and _matches_type(m):
            return m
        if _is_vendor(m) and _matches_type(m):
            return m

    return None


def _find_missing_tools(ctx: BrainContext) -> list[tuple[int, int]]:
    """Check which essential tools are missing from backpack + equipment."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)

    owned_graphics: set[int] = set()
    if backpack:
        for it in world.items.values():
            if it.container == backpack:
                owned_graphics.add(it.graphic)
    for layer in (0x01, 0x02):
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
