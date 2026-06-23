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
    """Wait for vendor buy list to appear after requesting buy.

    The readiness signal is a NON-EMPTY list, not merely ``is not None``.
    ``vendor_buy_list`` is set to the empty list ``[]`` whenever trade state
    is cleared — on death (self_state._GHOST transition), on a 0xBF vendor
    close (handle_close_vendor), on a 0x1D Delete for the vendor, and by the
    buy/sell procedures between transactions. ``[] is not None`` is True, so an
    ``is not None`` wait returns success on a stale/cleared empty list the
    instant it polls — the caller then fires build_buy_items at the (stale)
    vendor_serial against no items, a doomed transaction. Mirror the proven
    discriminator in skills/trade/vendor._wait_for_buy_list, which waits for a
    truthy (populated) list.
    """
    ss = ctx.perception.self_state

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: bool(ss.vendor_buy_list),
            timeout=timeout,
        )
    else:
        for _ in range(int(timeout / 0.1)):
            if ss.vendor_buy_list:
                break
            await asyncio.sleep(0.1)
        ok = bool(ss.vendor_buy_list)

    if not ok:
        return ActionResult(success=False, message="Buy list timeout")

    return ActionResult(success=True, data={"buy_list": ss.vendor_buy_list})


async def wait_for_sell_list(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for vendor sell list to appear after requesting sell.

    Readiness is a NON-EMPTY list — see wait_for_buy_list. ``vendor_sell_list``
    is reset to ``[]`` on death / vendor-close / Delete and between
    transactions, and ``[] is not None`` is True, so an ``is not None`` wait
    phantom-succeeds on a cleared list and the caller sells nothing against a
    stale vendor. Mirror skills/trade/vendor._wait_for_sell_list (truthy wait).
    """
    ss = ctx.perception.self_state

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: bool(ss.vendor_sell_list),
            timeout=timeout,
        )
    else:
        for _ in range(int(timeout / 0.1)):
            if ss.vendor_sell_list:
                break
            await asyncio.sleep(0.1)
        ok = bool(ss.vendor_sell_list)

    if not ok:
        return ActionResult(success=False, message="Sell list timeout")

    return ActionResult(success=True, data={"sell_list": ss.vendor_sell_list})
