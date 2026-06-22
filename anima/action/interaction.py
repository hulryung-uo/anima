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

    Returns True only when the lift actually took: on a server lift-reject
    (0x27 LiftRej — out of range, locked-down, too heavy, server busy) the
    item never leaves its slot, so firing the follow-up DropItem (0x08)
    against an item we are not holding drops nothing (or dumps it on the
    floor) while still reporting success. ``move_item_on_ground`` keys its
    whole drag loop off this return: a phantom True makes it walk on as if
    the item advanced (it did not), so it re-drags from the same spot every
    iteration and burns its entire step budget without moving the item.
    Mirror the proven guard in ``drag_to_container`` /
    ``anima.actions.inventory.drag_drop``: snapshot the source slot and bail
    out before the drop when the item did not move.
    """
    ss = ctx.perception.self_state
    drop_dist = max(abs(x - ss.x), abs(y - ss.y))
    if drop_dist > _DRAG_DROP_RANGE:
        logger.warning("drag_drop_too_far", pos=f"({x},{y})", dist=drop_dist)
        return False

    world = ctx.perception.world
    pre_state = None
    if serial in world.items:
        before = world.items[serial]
        pre_state = (before.container, before.x, before.y, before.z, before.amount)

    await ctx.conn.send_packet(build_pick_up(serial, amount))
    await asyncio.sleep(0.3)

    if pre_state is not None and serial in world.items:
        after = world.items[serial]
        if (after.container, after.x, after.y, after.z, after.amount) == pre_state:
            logger.debug(
                "drag_to_ground_lift_rejected",
                serial=f"0x{serial:08X}", amount=amount, pos=f"({x},{y},{z})",
            )
            return False

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
    # Snapshot pre-lift state. On a successful lift ServUO removes the item
    # from view (it moves into the mobile's Holding), so the world entry
    # either disappears or changes; a PARTIAL lift from a stack leaves the
    # source serial in place but decrements its ``amount``. A REJECTED lift
    # (0x27 LiftRej — out of range, locked-down, too heavy, server busy)
    # leaves the item exactly where it was. Without this check a rejected
    # lift still fired the follow-up DropItem (0x08) against an item we are
    # not holding — the server drops it on the floor or silently ignores it —
    # while this returns True, so the caller (bank/restock/store flows) marks
    # the item moved and moves on, stranding it. Mirror the proven guard in
    # ``anima.actions.inventory.drag_drop``: bail out before the drop when the
    # item did not move.
    world = ctx.perception.world
    pre_state = None
    if serial in world.items:
        before = world.items[serial]
        pre_state = (before.container, before.x, before.y, before.z, before.amount)

    await ctx.conn.send_packet(build_pick_up(serial, amount))
    await asyncio.sleep(0.3)

    if pre_state is not None and serial in world.items:
        after = world.items[serial]
        if (after.container, after.x, after.y, after.z, after.amount) == pre_state:
            logger.debug(
                "drag_to_container_lift_rejected",
                serial=f"0x{serial:08X}", amount=amount,
                container=f"0x{container_serial:08X}",
            )
            return False

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

        # Wait for stamina to recover if low (heavy items drain stam).
        # Bounded like the go_to fatigue wait in anima.action.movement: a
        # desynced/stuck stamina value (e.g. the server stops sending 0x11
        # stat updates) must never hang the drag forever, so cap the wait at
        # a fixed number of 1s ticks and move on regardless.
        if ss.stam_max > 0 and ss.stam < ss.stam_max * 0.3:
            logger.info(
                "move_item_stam_wait",
                serial=f"0x{serial:08X}", stam=ss.stam, stam_max=ss.stam_max,
            )
            for _ in range(30):
                await asyncio.sleep(1.0)
                if not (ss.stam_max > 0 and ss.stam < ss.stam_max * 0.3):
                    break
            else:
                logger.info("move_item_stam_timeout", serial=f"0x{serial:08X}", stam=ss.stam)

        # Walk to the drop position to continue dragging
        await go_to(ctx, next_x, next_y)

    return False
