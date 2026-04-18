"""BuyFromVendor procedure — buy tools from NPC vendor via context menu.

Improvements over the original:
- Priority-based tool selection: buys whatever the agent is missing most
  (tongs > tinker tools > pickaxe > shovel), not just the first tool found.
- Per-vendor item blacklist with TTL: if buying a specific item serial
  fails, it's blacklisted for 2 minutes so the next attempt picks a
  different item from the vendor's inventory.
- Gold polling: waits up to 2s for the server's stats update instead of
  a fixed 1s sleep, reducing false "gold unchanged" failures.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import count_items, find_in_backpack
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.vendor import (
    _CLILOC_VENDOR_BUY,
    _mark_refused,
    _request_context_menu_entry,
    _find_vendor,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Blacklisted item serials expire after this many seconds.
# Vendor inventory refreshes periodically, so a short TTL is fine.
_BUY_ITEM_BLACKLIST_TTL = 120.0

# Priority-ordered tool categories: most critical first.
# Tongs unlock the entire crafting chain; tinker tools let you craft
# replacement tools; pickaxes are consumed during mining.
_TOOL_PRIORITIES: list[tuple[str, set[int]]] = [
    ("tongs",        {0x0FBB, 0x0FBC}),
    ("tinker_tools", {0x1EB8, 0x1EBC}),
    ("pickaxe",      {0x0E85, 0x0E86}),
    ("shovel",       {0x0F39}),
]

# All tool graphics combined (for fallback "any tool" search).
_ALL_TOOL_GRAPHICS: set[int] = set()
for _, gfx in _TOOL_PRIORITIES:
    _ALL_TOOL_GRAPHICS |= gfx

# Always buy enough to reach this count. Prevents the agent from
# running out mid-expedition and having to interrupt mining for a
# vendor trip after every single tool wears out.
TOOL_MIN_STOCK = 3

# Freshness window for the bank_balance cache written by
# check_bank_balance. Matches the planner's 10-minute window at
# `anima/planner/planner.py` Priority 4c.
_BANK_CACHE_TTL = 600.0

# Minimum funds (backpack + bank) required before we try to buy.
# Matches the cheapest tool price (pickaxe ~11gp) with a small margin.
_MIN_PURCHASE_FUNDS = 10


def _fresh_bank_amount(ctx: "AgentContext") -> int:
    """Return bank gold from the cache if fresh, else 0.

    The bank_balance cache is populated by the check_bank_balance
    procedure (ctx.blackboard["bank_balance"] = {"amount", "ts"}).
    """
    bal_cache = ctx.blackboard.get("bank_balance") or {}
    amount = bal_cache.get("amount")
    ts = bal_cache.get("ts", 0)
    if amount is None:
        return 0
    if time.time() - ts > _BANK_CACHE_TTL:
        return 0
    return amount


def _available_funds(ctx: "AgentContext") -> int:
    """Backpack gold + fresh bank gold.

    Vendor purchases on this shard deduct from bank when the backpack
    is insufficient, so the total available is what governs which
    tools are affordable — not backpack alone.
    """
    return ctx.perception.self_state.gold + _fresh_bank_amount(ctx)


def _has_purchase_funds(ctx: "AgentContext") -> bool:
    """True if combined backpack + fresh bank balance >= _MIN_PURCHASE_FUNDS.

    An empty backpack alone does not block the buy because the shard
    falls back to bank gold; only "both empty" is a true blocker.
    """
    return _available_funds(ctx) >= _MIN_PURCHASE_FUNDS



def _needed_tool_graphics(ctx: AgentContext) -> list[set[int]]:
    """Return tool graphic sets the agent needs to restock, in priority order.

    A tool category is "needed" when the backpack has fewer than
    TOOL_MIN_STOCK of that type.
    """
    needed: list[set[int]] = []
    for _name, graphics in _TOOL_PRIORITIES:
        count = len(find_in_backpack(ctx, graphics))
        if count < TOOL_MIN_STOCK:
            needed.append(graphics)
    return needed


def _tool_restock_qty(ctx: AgentContext, graphic: int) -> int:
    """How many of this specific graphic to buy to reach TOOL_MIN_STOCK."""
    # Count all tools sharing the same category (e.g., pickaxe has 2 graphics)
    for _name, graphics in _TOOL_PRIORITIES:
        if graphic in graphics:
            count = len(find_in_backpack(ctx, graphics))
            return max(1, TOOL_MIN_STOCK - count)
    return 1


class BuyFromVendor(Procedure):
    name = "buy_from_vendor"
    description = "Buy tools from a nearby NPC vendor."

    _TOOL_VENDOR_TYPES: set[str] = {"tinker", "provisioner", "miner"}

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not _has_purchase_funds(ctx):
            return False
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.9:
            return False
        return _find_vendor(ctx, vendor_types=self._TOOL_VENDOR_TYPES) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        gold_before = ss.gold

        vendor = _find_vendor(ctx, vendor_types=self._TOOL_VENDOR_TYPES)
        if not vendor:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no vendor nearby",
            )

        vendor_name = vendor.name or "vendor"

        if not _has_purchase_funds(ctx):
            bal_cache = ctx.blackboard.get("bank_balance") or {}
            bal_amount = bal_cache.get("amount")
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=(
                    f"not enough gold (backpack={ss.gold}gp, "
                    f"bank={bal_amount}gp)"
                ),
            )

        logger.info(
            "buy_attempt",
            vendor=vendor_name,
            vendor_pos=f"({vendor.x},{vendor.y})",
            gold=ss.gold,
        )

        # Request buy menu via context menu
        ok = await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_BUY)
        if not ok:
            # Vendor's context menu lacks the Buy entry (e.g. guildmistresses
            # whose title looks like a vendor but who only teach skills).
            # Mark refused so the planner stops picking the same NPC every
            # tick — matches the sell_no_menu path in sell_to_vendor.
            _mark_refused(ctx, vendor.serial)
            logger.warning(
                "buy_no_menu",
                vendor=vendor_name,
                reason="context menu did not have Buy option",
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"{vendor_name} has no Buy option — marked refused",
            )

        # Wait for buy list
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if ss.vendor_buy_list:
                break
            await asyncio.sleep(0.2)

        if not ss.vendor_buy_list:
            logger.warning("buy_no_list", vendor=vendor_name)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"buy list did not appear from {vendor_name}",
            )

        from anima.client.packets import build_buy_items
        buy_list = ss.vendor_buy_list

        # Log vendor inventory
        logger.info(
            "buy_list_received",
            vendor=vendor_name,
            total_items=len(buy_list),
            available=[
                f"{it.name or f'0x{it.graphic:04X}'} @{it.price}gp"
                for it in buy_list[:15]
            ],
        )

        # --- Per-vendor item blacklist ---
        blacklist: dict[int, dict[int, float]] = ctx.blackboard.get(
            "_buy_failed_items", {}
        )
        now = time.monotonic()
        vendor_bl = blacklist.get(vendor.serial, {})
        active_bl = {s for s, t in vendor_bl.items() if now - t < _BUY_ITEM_BLACKLIST_TTL}

        # --- Priority-based tool selection ---
        # 1) Try each missing-tool category in priority order.
        # 2) If nothing missing matches, try ANY affordable tool.
        # Affordability is backpack + bank because the shard falls back
        # to bank gold when the pouch can't cover the price.
        budget = _available_funds(ctx)
        target_item = None
        needed = _needed_tool_graphics(ctx)

        for graphics_set in needed:
            for item in buy_list:
                if item.serial in active_bl:
                    continue
                if item.graphic in graphics_set and 0 < item.price <= budget:
                    target_item = item
                    break
            if target_item:
                break

        # Fallback: restock any tool below TOOL_MIN_STOCK
        if not target_item:
            for item in buy_list:
                if item.serial in active_bl:
                    continue
                if item.graphic in _ALL_TOOL_GRAPHICS and 0 < item.price <= budget:
                    target_item = item
                    break

        if not target_item:
            logger.warning(
                "buy_no_tools_available",
                vendor=vendor_name,
                gold=ss.gold,
                budget=budget,
                needed=[n for g in needed for n in [f"0x{x:04X}" for x in g]],
                blacklisted=len(active_bl),
            )
            _mark_refused(ctx, vendor.serial)
            ss.vendor_buy_list = []
            ss.vendor_serial = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"no affordable tools at {vendor_name} "
                        f"(blacklisted={len(active_bl)})",
            )

        target_name = target_item.name or f"0x{target_item.graphic:04X}"

        # Buy enough to reach TOOL_MIN_STOCK, capped by combined funds.
        buy_qty = _tool_restock_qty(ctx, target_item.graphic)
        total_cost = target_item.price * buy_qty
        if total_cost > budget and buy_qty > 1:
            buy_qty = max(1, budget // target_item.price)
            total_cost = target_item.price * buy_qty

        logger.info(
            "buy_sending",
            vendor=vendor_name,
            item=target_name,
            graphic=f"0x{target_item.graphic:04X}",
            price=target_item.price,
            qty=buy_qty,
            total_cost=total_cost,
            gold=gold_before,
        )

        # Snapshot backpack count so we can detect success even when the
        # server pays from bank (ss.gold unchanged) instead of the pouch.
        target_graphic_set = {target_item.graphic}
        qty_before = count_items(ctx, target_graphic_set)
        bank_before = _fresh_bank_amount(ctx)

        await ctx.conn.send_packet(build_buy_items(
            ss.vendor_serial,
            [(target_item.serial, buy_qty)],
        ))

        # Poll up to 2s for either signal of success:
        #   - backpack gold decreases (pouch payment)
        #   - backpack item count increases (item delivered)
        # Either suffices; the shard picks the payment path based on
        # which pocket can cover the cost.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if ss.gold != gold_before:
                break
            if count_items(ctx, target_graphic_set) > qty_before:
                break
            await asyncio.sleep(0.2)

        gold_after = ss.gold
        gold_spent = max(0, gold_before - gold_after)
        qty_after = count_items(ctx, target_graphic_set)
        item_gained = qty_after > qty_before

        # Clear vendor state
        ss.vendor_buy_list = []
        ss.vendor_serial = 0

        logger.info(
            "buy_result",
            vendor=vendor_name,
            item=target_name,
            price=target_item.price,
            gold_before=gold_before,
            gold_after=gold_after,
            gold_spent=gold_spent,
            qty_before=qty_before,
            qty_after=qty_after,
            item_gained=item_gained,
        )

        if not item_gained and gold_spent == 0:
            logger.warning(
                "buy_failed_no_delivery",
                vendor=vendor_name,
                item=target_name,
                item_serial=target_item.serial,
                price=target_item.price,
                bank_before=bank_before,
            )
            # Blacklist this item serial for this vendor
            bl = ctx.blackboard.setdefault("_buy_failed_items", {})
            vendor_bl = bl.setdefault(vendor.serial, {})
            vendor_bl[target_item.serial] = time.monotonic()
            _mark_refused(ctx, vendor.serial)
            # Disable buy attempts for 10 min ONLY when we believed we had
            # funds but nothing came back. If the bank cache said zero
            # (bank_before == 0) and backpack was empty too, can_start
            # would have blocked us — reaching here with item_gained=False
            # means our funds belief was wrong, so cooling off avoids a
            # tight retry loop while the cache refreshes.
            ctx.blackboard["_buy_disabled_until"] = time.time() + 600
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Buy {target_name} from {vendor_name} failed — "
                        f"no item delivered (backpack {gold_before}gp, "
                        f"bank cache {bank_before}gp, buy disabled 10min)",
            )

        # Success — clear blacklist for this vendor
        bl = ctx.blackboard.get("_buy_failed_items", {})
        bl.pop(vendor.serial, None)
        # Invalidate bank cache if payment likely came from bank so the
        # next buy re-checks the real balance instead of over-spending.
        if item_gained and gold_spent == 0 and bank_before > 0:
            bal_cache = ctx.blackboard.get("bank_balance")
            if bal_cache:
                bal_cache["amount"] = max(
                    0, bal_cache.get("amount", 0) - target_item.price * buy_qty,
                )
        pay_note = f"{gold_spent}gp" if gold_spent > 0 else "bank"
        return ProcedureResult(
            success=True,
            message=f"Bought {target_name} from {vendor_name} for {pay_note}",
            gold_changed=-gold_spent,
        )
