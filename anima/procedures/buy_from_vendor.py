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

from anima.actions.inventory import find_in_backpack
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.vendor import (
    _CLILOC_VENDOR_BUY,
    _mark_refused,
    _request_context_menu_entry,
    _find_vendor,
    _wait_for_gold_change,
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


def _needed_tool_graphics(ctx: AgentContext) -> list[set[int]]:
    """Return tool graphic sets the agent is missing, in priority order."""
    needed: list[set[int]] = []
    for _name, graphics in _TOOL_PRIORITIES:
        if not find_in_backpack(ctx, graphics):
            needed.append(graphics)
    return needed


class BuyFromVendor(Procedure):
    name = "buy_from_vendor"
    description = "Buy tools from a nearby NPC vendor."

    # Tinkers and provisioners both sell basic tools.
    _TOOL_VENDOR_TYPES: set[str] = {"tinker", "provisioner"}

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold < 10:
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

        if ss.gold < 10:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"not enough gold ({ss.gold}gp)",
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
            logger.warning("buy_no_menu", vendor=vendor_name)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"{vendor_name} did not respond to buy request",
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
        target_item = None
        needed = _needed_tool_graphics(ctx)

        for graphics_set in needed:
            for item in buy_list:
                if item.serial in active_bl:
                    continue
                if item.graphic in graphics_set and 0 < item.price <= ss.gold:
                    target_item = item
                    break
            if target_item:
                break

        # Fallback: buy any affordable tool (even if we already have one)
        if not target_item:
            for item in buy_list:
                if item.serial in active_bl:
                    continue
                if item.graphic in _ALL_TOOL_GRAPHICS and 0 < item.price <= ss.gold:
                    target_item = item
                    break

        if not target_item:
            logger.warning(
                "buy_no_tools_available",
                vendor=vendor_name,
                gold=ss.gold,
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
        logger.info(
            "buy_sending",
            vendor=vendor_name,
            item=target_name,
            graphic=f"0x{target_item.graphic:04X}",
            price=target_item.price,
            gold=gold_before,
        )

        await ctx.conn.send_packet(build_buy_items(
            ss.vendor_serial,
            [(target_item.serial, 1)],
        ))

        # --- Gold polling (up to 2s) ---
        await _wait_for_gold_change(ss, gold_before, timeout=2.0)

        gold_after = ss.gold
        gold_spent = max(0, gold_before - gold_after)

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
        )

        if gold_spent == 0:
            logger.warning(
                "buy_failed_gold_unchanged",
                vendor=vendor_name,
                item=target_name,
                item_serial=target_item.serial,
                price=target_item.price,
            )
            # Blacklist this item serial for this vendor
            bl = ctx.blackboard.setdefault("_buy_failed_items", {})
            vendor_bl = bl.setdefault(vendor.serial, {})
            vendor_bl[target_item.serial] = time.monotonic()
            _mark_refused(ctx, vendor.serial)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Buy {target_name} from {vendor_name} failed — "
                        f"gold unchanged ({gold_before}gp→{gold_after}gp)",
            )

        # Success — clear blacklist for this vendor
        bl = ctx.blackboard.get("_buy_failed_items", {})
        bl.pop(vendor.serial, None)
        return ProcedureResult(
            success=True,
            message=f"Bought {target_name} from {vendor_name} for {gold_spent}gp",
            gold_changed=-gold_spent,
        )
