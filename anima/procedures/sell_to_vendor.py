"""SellToVendor procedure — sell items to NPC vendor via context menu."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.vendor import (
    _CLILOC_VENDOR_SELL,
    _mark_refused,
    _request_context_menu_entry,
    _find_vendor,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class SellToVendor(Procedure):
    name = "sell_to_vendor"
    description = "Sell items to a nearby NPC vendor."

    async def can_start(self, ctx: AgentContext) -> bool:
        # _find_vendor already skips refused vendors via _is_refused()
        vendor = _find_vendor(ctx)
        return vendor is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state

        vendor = _find_vendor(ctx)
        if not vendor:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no vendor nearby",
            )

        # Request sell menu via context menu
        ok = await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_SELL)
        if not ok:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="vendor did not respond to sell request",
            )

        # Wait for sell list, watch for rejection speech
        _rejected = {"hit": False}
        def _check_rejection(_topic: str, data: dict) -> None:
            text = data.get("text", "").lower()
            if "nothing i would be interested" in text or "not interested" in text:
                _rejected["hit"] = True

        sub = None
        if ctx.bus:
            sub = ctx.bus.subscribe("avatar.speech_heard", _check_rejection)

        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if ss.vendor_sell_list or _rejected["hit"]:
                break
            await asyncio.sleep(0.2)

        if sub and ctx.bus:
            ctx.bus.unsubscribe(sub)

        if _rejected["hit"]:
            # This vendor doesn't want our items — mark and try elsewhere
            vendor_name = vendor.name or "vendor"
            _mark_refused(ctx, vendor.serial)
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"{vendor_name} not interested in our items",
            )

        if not ss.vendor_sell_list:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="sell list did not appear",
            )

        # Sell all items EXCEPT tools we need to keep
        from anima.client.packets import build_sell_items
        from anima.skills.trade.banking import KEEP_GRAPHICS
        _KEEP_GRAPHICS = KEEP_GRAPHICS  # pickaxes, hatchets, saws, tinker tools, shovels, bandages
        sell_list = ss.vendor_sell_list
        if sell_list:
            items_to_sell = [
                (item.serial, item.amount) for item in sell_list
                if item.graphic not in _KEEP_GRAPHICS
            ]
            await ctx.conn.send_packet(build_sell_items(
                vendor.serial,
                items_to_sell,
            ))
            await asyncio.sleep(1.0)

            return ProcedureResult(
                success=True,
                message=f"Sold {len(items_to_sell)} items to {vendor.name or 'vendor'}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message="empty sell list",
        )
