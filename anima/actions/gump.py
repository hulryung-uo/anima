"""Gump action primitives — wait for gump, click buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_gump_response

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.perception.gump import GumpData

logger = structlog.get_logger()


async def wait_for_gump(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for a gump to appear."""
    ss = ctx.perception.self_state

    def _has_gump() -> bool:
        return bool(ss.gumps)

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(_has_gump, timeout=timeout)
    else:
        import asyncio
        for _ in range(int(timeout / 0.1)):
            if _has_gump():
                break
            await asyncio.sleep(0.1)
        ok = _has_gump()

    if not ok:
        return ActionResult(success=False, message="Gump timeout")

    # Return the most recent gump
    gump_id = max(ss.gumps.keys())
    gump = ss.gumps[gump_id]
    return ActionResult(
        success=True,
        data={"gump_id": gump_id, "gump": gump},
    )


async def click_gump_button(
    ctx: AgentContext,
    gump: GumpData,
    button_id: int,
    switches: list[int] | None = None,
    text_entries: list[tuple[int, str]] | None = None,
) -> ActionResult:
    """Click a button on an open gump."""
    pkt = build_gump_response(
        serial=gump.serial,
        gump_id=gump.gump_id,
        button_id=button_id,
        switches=switches or [],
        text_entries=text_entries or [],
    )
    await ctx.conn.send_packet(pkt)

    # Remove the gump from state
    ss = ctx.perception.self_state
    ss.gumps.pop(gump.gump_id, None)

    logger.debug(
        "gump_button_clicked",
        gump_id=f"0x{gump.gump_id:08X}",
        button=button_id,
    )
    return ActionResult(success=True, data={"button_id": button_id})


async def craft_via_gump(
    ctx: AgentContext,
    tool_serial: int,
    category_button: int,
    item_button: int,
    timeout: float = 3.0,
) -> ActionResult:
    """Composite: use tool → wait gump → click category → wait gump → click item.

    Used by blacksmithy, tinkering, carpentry crafting gumps.
    """
    from anima.client.packets import build_double_click

    # Open crafting gump
    await ctx.conn.send_packet(build_double_click(tool_serial))

    # Wait for crafting gump
    result = await wait_for_gump(ctx, timeout=timeout)
    if not result.success:
        return ActionResult(success=False, message="Crafting gump did not open")

    gump = result.data["gump"]

    # Click category
    result = await click_gump_button(ctx, gump, category_button)
    if not result.success:
        return result

    # Wait for category sub-gump
    result = await wait_for_gump(ctx, timeout=timeout)
    if not result.success:
        return ActionResult(success=False, message="Category gump did not open")

    gump = result.data["gump"]

    # Click specific item
    result = await click_gump_button(ctx, gump, item_button)
    if not result.success:
        return result

    return ActionResult(success=True, message="Craft command sent")
