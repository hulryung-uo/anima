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

    # A two-handed weapon occupies BOTH hands. ServUO BaseWeapon.cs
    # CheckConflictingLayer refuses a two-hander outright when the
    # one-handed layer is full ("You already have something in both
    # hands."), so the EquipItem never takes. equip_item below would
    # nonetheless PickUp the weapon first — lifting it loose onto the
    # cursor / out of the pack — and then report a *soft success* on the
    # rejected wear, leaving the agent disarmed with the two-hander
    # stranded on the cursor. Bail out before lifting anything when the
    # off-hand is occupied so the caller's existing weapon stays equipped.
    if two_handed and ss.equipment.get(LAYER_ONE_HANDED):
        return ActionResult(
            success=False,
            message="One-handed hand occupied — can't equip a two-handed weapon",
        )

    weapons = find_in_backpack(ctx, graphics)
    if not weapons:
        return ActionResult(success=False, message="No weapon in backpack")
    return await equip_item(ctx, weapons[0].serial, layer)


# Shield item graphics — verified against ServUO
# Scripts/Items/Equipment/Armor/*.cs ``base(0x....)`` constructors.
SHIELD_GRAPHICS = {
    0x1B72,  # BronzeShield
    0x1B73,  # Buckler
    0x1B74,  # MetalKiteShield
    0x1B76,  # HeaterShield
    0x1B78,  # WoodenKiteShield
    0x1B7A,  # WoodenShield
    0x1B7B,  # MetalShield
    0x1BC3,  # ChaosShield
    0x1BC4,  # OrderShield
}


async def equip_shield_from_pack(ctx: AgentContext) -> ActionResult:
    """Find a shield in the backpack and equip it to the left hand (layer 2).

    Wearing a shield makes every melee exchange roll Parrying on
    ServUO, adding a passive COMBAT skill stream while hunting.
    """
    from anima.actions.inventory import find_in_backpack

    ss = ctx.perception.self_state
    if ss.equipment.get(LAYER_TWO_HANDED):
        return ActionResult(success=True, message="Left hand already occupied")

    shields = find_in_backpack(ctx, SHIELD_GRAPHICS)
    if not shields:
        return ActionResult(success=False, message="No shield in backpack")
    return await equip_item(ctx, shields[0].serial, LAYER_TWO_HANDED)
