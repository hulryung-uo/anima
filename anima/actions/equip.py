"""Equip action primitives — lift an item from the pack and wear it.

UO equip flow: PickUp (0x07) the item, then EquipItem (0x13) onto a
layer of the wearer. The server confirms via the equipment update
packet (0x2E), which the perception layer mirrors into
SelfState.equipment (layer → serial).

Common layers: 1=right hand (one-handed weapon), 2=left hand
(two-handed / shield), 5=chest, 6=head, 0x15=backpack.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_equip_item, build_pick_up

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

LAYER_ONE_HANDED = 1
LAYER_TWO_HANDED = 2


async def equip_item(
    ctx: AgentContext,
    item_serial: int,
    layer: int,
    timeout: float = 2.0,
) -> ActionResult:
    """Pick up an item and equip it on the given layer, verify via 0x2E."""
    ss = ctx.perception.self_state
    if ss.equipment.get(layer) == item_serial:
        return ActionResult(success=True, message="Already equipped")

    await ctx.conn.send_packet(build_pick_up(item_serial, 1))
    await asyncio.sleep(0.3)
    await ctx.conn.send_packet(build_equip_item(item_serial, layer, ss.serial))

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        if ss.equipment.get(layer) == item_serial:
            return ActionResult(
                success=True,
                data={"layer": layer, "serial": item_serial},
            )
    # Server may not echo 0x2E for self-equips on every shard config —
    # report soft success so callers can verify by behavior instead.
    logger.debug("equip_unverified", serial=f"0x{item_serial:08X}", layer=layer)
    return ActionResult(
        success=True,
        message="Equip sent (unverified)",
        data={"layer": layer, "serial": item_serial, "verified": False},
    )


async def equip_weapon_from_pack(
    ctx: AgentContext,
    graphics: set[int],
    two_handed: bool = False,
) -> ActionResult:
    """Find a weapon by graphic in the backpack and equip it."""
    from anima.actions.inventory import find_in_backpack

    ss = ctx.perception.self_state
    layer = LAYER_TWO_HANDED if two_handed else LAYER_ONE_HANDED
    if ss.equipment.get(layer):
        return ActionResult(success=True, message="Hand already occupied")

    weapons = find_in_backpack(ctx, graphics)
    if not weapons:
        return ActionResult(success=False, message="No weapon in backpack")
    return await equip_item(ctx, weapons[0].serial, layer)
