"""Interaction actions: use items, double-click targets."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()


async def use_item(ctx: BrainContext, serial: int) -> Status:
    """Double-click an item to use it."""
    from anima.brain.behavior_tree import Status

    await ctx.conn.send_packet(build_double_click(serial))
    logger.debug("use_item", serial=f"0x{serial:08X}")
    return Status.SUCCESS


async def double_click(ctx: BrainContext, serial: int) -> Status:
    """Send a double-click on any entity."""
    from anima.brain.behavior_tree import Status

    await ctx.conn.send_packet(build_double_click(serial))
    logger.debug("double_click", serial=f"0x{serial:08X}")
    return Status.SUCCESS
