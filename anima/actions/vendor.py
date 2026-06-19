"""Vendor action primitives — context menu, buy/sell lists."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


async def request_context_menu(
    ctx: AgentContext,
    serial: int,
    timeout: float = 3.0,
) -> ActionResult:
    """Request and wait for a context menu on an entity."""
    from anima.client.packets import build_context_menu_request

    ss = ctx.perception.self_state
    ss.context_menu = None
    await ctx.conn.send_packet(build_context_menu_request(serial))

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: ss.context_menu is not None,
            timeout=timeout,
        )
    else:
        for _ in range(int(timeout / 0.1)):
            if ss.context_menu is not None:
                break
            await asyncio.sleep(0.1)
        ok = ss.context_menu is not None

    if not ok:
        return ActionResult(success=False, message="Context menu timeout")

    return ActionResult(success=True, data={"menu": ss.context_menu})


async def select_context_menu_entry(
    ctx: AgentContext,
    serial: int,
    entry_index: int,
) -> ActionResult:
    """Select an entry from the context menu by index."""
    from anima.client.packets import build_context_menu_selection

    await ctx.conn.send_packet(build_context_menu_selection(serial, entry_index))
    logger.debug("context_menu_selected", serial=f"0x{serial:08X}", index=entry_index)
    return ActionResult(success=True)


async def wait_for_buy_list(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for vendor buy list to appear after requesting buy."""
    ss = ctx.perception.self_state

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: ss.vendor_buy_list is not None,
            timeout=timeout,
        )
    else:
        for _ in range(int(timeout / 0.1)):
            if ss.vendor_buy_list is not None:
                break
            await asyncio.sleep(0.1)
        ok = ss.vendor_buy_list is not None

    if not ok:
        return ActionResult(success=False, message="Buy list timeout")

    return ActionResult(success=True, data={"buy_list": ss.vendor_buy_list})


async def wait_for_sell_list(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for vendor sell list to appear after requesting sell."""
    ss = ctx.perception.self_state

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: ss.vendor_sell_list is not None,
            timeout=timeout,
        )
    else:
        for _ in range(int(timeout / 0.1)):
            if ss.vendor_sell_list is not None:
                break
            await asyncio.sleep(0.1)
        ok = ss.vendor_sell_list is not None

    if not ok:
        return ActionResult(success=False, message="Sell list timeout")

    return ActionResult(success=True, data={"sell_list": ss.vendor_sell_list})
