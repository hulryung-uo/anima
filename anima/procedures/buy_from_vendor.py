"""BuyFromVendor procedure — buy items from NPC vendor via context menu."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.vendor import (
    _CLILOC_VENDOR_BUY,
    _request_context_menu_entry,
    _find_vendor,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class BuyFromVendor(Procedure):
    name = "buy_from_vendor"
    description = "Buy items from a nearby NPC vendor."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold < 10:
            return False
        return _find_vendor(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        gold_before = ss.gold

        vendor = _find_vendor(ctx)
        if not vendor:
            logger.info("buy_vendor_not_found", pos=f"({ss.x},{ss.y})")
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no vendor nearby",
            )

        vendor_name = vendor.name or "vendor"

        if ss.gold < 10:
            logger.info("buy_no_gold", vendor=vendor_name, gold=ss.gold)
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"not enough gold ({ss.gold}gp) to buy from {vendor_name}",
            )

        logger.info(
            "buy_attempt",
            vendor=vendor_name,
            vendor_pos=f"({vendor.x},{vendor.y})",
            player_pos=f"({ss.x},{ss.y})",
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

        # Log all items the vendor sells
        available_items: list[str] = []
        for item in buy_list:
            name = item.name or f"0x{item.graphic:04X}"
            available_items.append(f"{name} @{item.price}gp")

        logger.info(
            "buy_list_received",
            vendor=vendor_name,
            total_items=len(buy_list),
            available=available_items,
        )

        # Prioritize buying mining tools (pickaxe/shovel) if we need one
        from anima.skills.gathering.mine import PICKAXE_GRAPHICS
        SHOVEL_GRAPHICS = {0x0F39}
        TOOL_GRAPHICS = PICKAXE_GRAPHICS | SHOVEL_GRAPHICS

        target_item = None
        for item in buy_list:
            if item.graphic in TOOL_GRAPHICS:
                if item.price > 0 and item.price <= ss.gold:
                    target_item = item
                    break

        # Fallback: buy first affordable item (must cost > 0)
        if not target_item and buy_list:
            for item in buy_list:
                if item.price > 0 and item.price <= ss.gold:
                    target_item = item
                    break

        if not target_item:
            # Log why nothing was affordable
            zero_price = [it.name or f"0x{it.graphic:04X}" for it in buy_list if it.price == 0]
            too_expensive = [
                f"{it.name or f'0x{it.graphic:04X}'} @{it.price}gp"
                for it in buy_list if it.price > ss.gold
            ]
            logger.warning(
                "buy_nothing_affordable",
                vendor=vendor_name,
                gold=ss.gold,
                zero_price_items=zero_price if zero_price else None,
                too_expensive=too_expensive[:5] if too_expensive else None,
            )
            ss.vendor_buy_list = []
            ss.vendor_serial = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"nothing affordable from {vendor_name} (gold={ss.gold}gp, "
                        f"{len(zero_price)} items @0gp, {len(too_expensive)} too expensive)",
            )

        target_name = target_item.name or f"0x{target_item.graphic:04X}"
        logger.info(
            "buy_sending",
            vendor=vendor_name,
            item=target_name,
            graphic=f"0x{target_item.graphic:04X}",
            price=target_item.price,
            gold_before=gold_before,
        )

        await ctx.conn.send_packet(build_buy_items(
            vendor.serial,
            [(target_item.serial, 1)],
        ))
        await asyncio.sleep(1.0)

        # Verify purchase
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
                "buy_no_gold_spent",
                vendor=vendor_name,
                item=target_name,
                price=target_item.price,
                reason="sent buy packet but gold unchanged — purchase may have failed",
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Buy {target_name} from {vendor_name} failed — "
                        f"gold unchanged ({gold_before}gp→{gold_after}gp)",
                details={
                    "vendor": vendor_name,
                    "item": target_name,
                    "price": target_item.price,
                    "gold_before": gold_before,
                    "gold_after": gold_after,
                },
            )

        return ProcedureResult(
            success=True,
            message=f"Bought {target_name} from {vendor_name} for {gold_spent}gp",
            gold_changed=-gold_spent,
            details={
                "vendor": vendor_name,
                "item": target_name,
                "price": target_item.price,
                "gold_spent": gold_spent,
            },
        )
