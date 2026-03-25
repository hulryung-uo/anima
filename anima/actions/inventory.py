"""Inventory action primitives — find items, count, drag-drop."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_drop_item, build_pick_up

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.perception.world_state import ItemInfo

logger = structlog.get_logger()


def find_in_backpack(
    ctx: AgentContext,
    graphics: set[int],
) -> list[ItemInfo]:
    """Find items in backpack matching any of the given graphic IDs."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)  # Layer.BACKPACK
    if not backpack:
        return []

    return [
        item for item in world.items.values()
        if item.container == backpack and item.graphic in graphics
    ]


def count_items(ctx: AgentContext, graphics: set[int]) -> int:
    """Count total amount of items in backpack matching given graphic IDs."""
    return sum(item.amount for item in find_in_backpack(ctx, graphics))


async def drag_drop(
    ctx: AgentContext,
    item_serial: int,
    amount: int,
    target_serial: int,
    x: int = 0xFFFF,
    y: int = 0xFFFF,
    z: int = 0,
) -> ActionResult:
    """Lift item and drop onto target (container or ground).

    For container drops: target_serial=container, x/y=0xFFFF (random placement).
    For ground drops: target_serial=0xFFFF_FFFF, x/y/z=ground coords.
    """
    import asyncio

    await ctx.conn.send_packet(build_pick_up(item_serial, amount))
    await asyncio.sleep(0.3)
    await ctx.conn.send_packet(build_drop_item(item_serial, x, y, z, target_serial))
    await asyncio.sleep(0.3)

    logger.debug(
        "drag_drop",
        item=f"0x{item_serial:08X}",
        amount=amount,
        target=f"0x{target_serial:08X}",
    )
    return ActionResult(success=True)
