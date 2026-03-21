"""Interaction actions: use items, double-click, drag-and-drop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_drop_item, build_pick_up

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


_DRAG_DROP_RANGE = 2


async def drag_to_ground(
    ctx: "BrainContext",
    serial: int,
    amount: int,
    x: int,
    y: int,
    z: int,
) -> bool:
    """Pick up an item and drop it on the ground at (x, y, z).

    Drop target must be within 2 tiles of the player.
    Works for items in backpack or already on the ground.
    """
    ss = ctx.perception.self_state
    drop_dist = max(abs(x - ss.x), abs(y - ss.y))
    if drop_dist > _DRAG_DROP_RANGE:
        logger.warning("drag_drop_too_far", pos=f"({x},{y})", dist=drop_dist)
        return False
    await ctx.conn.send_packet(build_pick_up(serial, amount))
    await asyncio.sleep(0.3)
    await ctx.conn.send_packet(build_drop_item(serial, x, y, z))
    await asyncio.sleep(0.3)
    logger.debug(
        "drag_to_ground",
        serial=f"0x{serial:08X}", amount=amount, pos=f"({x},{y},{z})",
    )
    return True


async def drag_to_container(
    ctx: "BrainContext",
    serial: int,
    amount: int,
    container_serial: int,
) -> bool:
    """Pick up an item and drop it into a container."""
    await ctx.conn.send_packet(build_pick_up(serial, amount))
    await asyncio.sleep(0.3)
    await ctx.conn.send_packet(
        build_drop_item(serial, 0xFFFF, 0xFFFF, 0, container_serial)
    )
    await asyncio.sleep(0.3)
    logger.debug(
        "drag_to_container",
        serial=f"0x{serial:08X}", amount=amount,
        container=f"0x{container_serial:08X}",
    )
    return True


async def move_item_on_ground(
    ctx: "BrainContext",
    serial: int,
    amount: int,
    target_x: int,
    target_y: int,
    target_z: int,
) -> bool:
    """Move an item on the ground toward a target position.

    Drag-drop range is 2 tiles per step. If farther, the agent walks
    alongside the item, dragging it 2 tiles at a time.
    """
    from anima.action.movement import go_to

    world = ctx.perception.world
    ss = ctx.perception.self_state
    max_steps = 30

    for _ in range(max_steps):
        item = world.items.get(serial)
        if not item:
            logger.warning("move_item_lost", serial=f"0x{serial:08X}")
            return False

        ix, iy = item.x, item.y
        remaining = max(abs(target_x - ix), abs(target_y - iy))
        if remaining == 0:
            return True

        # Walk next to item first (must be within 2 tiles to pick up)
        player_to_item = max(abs(ix - ss.x), abs(iy - ss.y))
        if player_to_item > _DRAG_DROP_RANGE:
            await go_to(ctx, ix, iy)

        # Drop position: toward target, but within 2 tiles of player
        dx = max(-_DRAG_DROP_RANGE, min(_DRAG_DROP_RANGE, target_x - ss.x))
        dy = max(-_DRAG_DROP_RANGE, min(_DRAG_DROP_RANGE, target_y - ss.y))
        next_x, next_y = ss.x + dx, ss.y + dy

        ok = await drag_to_ground(ctx, serial, amount, next_x, next_y, target_z)
        if not ok:
            return False

        # Wait for stamina to recover if low (heavy items drain stam)
        while ss.stam_max > 0 and ss.stam < ss.stam_max * 0.3:
            await asyncio.sleep(1.0)

        # Walk to the drop position to continue dragging
        await go_to(ctx, next_x, next_y)

    return False
