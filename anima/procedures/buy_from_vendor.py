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
        if ss.gold < 50:
            return False
        return _find_vendor(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state

        vendor = _find_vendor(ctx)
        if not vendor:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no vendor nearby",
            )

        if ss.gold < 50:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="not enough gold",
            )

        # Request buy menu via context menu
        ok = await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_BUY)
        if not ok:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="vendor did not respond to buy request",
            )

        # Wait for buy list
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if ss.vendor_buy_list:
                break
            await asyncio.sleep(0.2)

        if not ss.vendor_buy_list:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="buy list did not appear",
            )

        # Buy first available item (simple strategy — planner can refine later)
        from anima.client.packets import build_buy_items
        buy_list = ss.vendor_buy_list
        if buy_list:
            # Buy 1 of first item
            first_item = buy_list[0]
            await ctx.conn.send_packet(build_buy_items(
                vendor.serial,
                [(first_item.serial, 1)],
            ))
            await asyncio.sleep(1.0)

            return ProcedureResult(
                success=True,
                message=f"Bought from {vendor.name or 'vendor'}",
                gold_changed=-first_item.price if hasattr(first_item, 'price') else 0,
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message="empty buy list",
        )
