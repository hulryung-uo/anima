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
    """Find items in the backpack matching any of the given graphic IDs.

    Scans recursively: items nested inside sub-containers (a bag of
    reagents, a pouch, an ore beetle's pack, etc.) that themselves sit in
    the backpack are included. Without this, counts of reagents/bandages/
    ore stored in an inner bag silently read as zero and procedures stall.
    """
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)  # Layer.BACKPACK
    if not backpack:
        return []

    # All container serials reachable from the backpack (the backpack plus
    # every sub-container nested within it, to any depth). BFS over the
    # parent->child item graph; ``seen`` guards against malformed worlds
    # with a container cycle so we never loop forever.
    reachable: set[int] = {backpack}
    frontier: list[int] = [backpack]
    seen: set[int] = set()
    while frontier:
        parent = frontier.pop()
        if parent in seen:
            continue
        seen.add(parent)
        # world.items is keyed by serial, so use the key as the item's serial
        # (robust even if the item object doesn't carry its own .serial).
        for serial, item in world.items.items():
            if item.container == parent and serial not in reachable:
                reachable.add(serial)
                frontier.append(serial)

    return [
        item for item in world.items.values()
        if item.container in reachable and item.graphic in graphics
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

    # Snapshot pre-lift state. On a successful lift ServUO removes the item
    # from view (it moves into the mobile's Holding), so the world entry
    # either disappears or changes. A rejected lift (0x27 LiftRej) leaves it
    # exactly where it was.
    world = ctx.perception.world
    pre_state = None
    if item_serial in world.items:
        before = world.items[item_serial]
        pre_state = (before.container, before.x, before.y, before.z)

    await ctx.conn.send_packet(build_pick_up(item_serial, amount))
    await asyncio.sleep(0.3)

    if pre_state is not None and item_serial in world.items:
        after = world.items[item_serial]
        if (after.container, after.x, after.y, after.z) == pre_state:
            logger.debug(
                "drag_drop_lift_rejected",
                item=f"0x{item_serial:08X}",
                amount=amount,
                target=f"0x{target_serial:08X}",
            )
            return ActionResult(
                success=False, message="lift rejected (item did not move)"
            )

    await ctx.conn.send_packet(build_drop_item(item_serial, x, y, z, target_serial))
    await asyncio.sleep(0.3)

    logger.debug(
        "drag_drop",
        item=f"0x{item_serial:08X}",
        amount=amount,
        target=f"0x{target_serial:08X}",
    )
    return ActionResult(success=True)
