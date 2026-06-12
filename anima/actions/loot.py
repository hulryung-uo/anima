"""Loot action primitives — open corpses and lift gold/valuables.

UO corpses are ground containers (graphic 0x2006). Double-clicking one
makes the server stream its contents (ContainerContent 0x3C); items
inside can then be lifted into the backpack via PickUp (0x07) +
DropItem (0x08). Standard container-open range is ~2 tiles.

The selection policy (gold always, plus vendor-sellable valuables)
mirrors the planner's deadlock-recovery looter
(``anima/planner/planner.py::_LootCorpses``) as an importable primitive.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_double_click, build_drop_item, build_pick_up

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.perception.world_state import ItemInfo

logger = structlog.get_logger()

CORPSE_GRAPHIC = 0x2006
GOLD_GRAPHIC = 0x0EED
LAYER_BACKPACK = 0x15
CONTENTS_WAIT_S = 1.5  # server round-trip for 0x3C after the open
LIFT_DELAY_S = 0.3  # pacing between pick_up / drop packets
WEIGHT_HEADROOM = 50  # stop looting this close to max weight


def _valuable_graphics() -> set[int]:
    """Graphics worth lifting besides gold (vendor-sellable / tools).

    Lazy imports — these modules import action primitives themselves,
    so resolving them at call time avoids import cycles.
    """
    from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
    from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
    from anima.skills.crafting.smelt import INGOT_GRAPHICS
    from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
    from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS

    shovel_graphics = {0x0F39}
    return (
        ORE_GRAPHICS
        | INGOT_GRAPHICS
        | PICKAXE_GRAPHICS
        | shovel_graphics
        | TINKER_TOOLS_GRAPHICS
        | TONGS_GRAPHICS
        | set(ITEM_VENDOR_MAP.keys())
    )


def find_corpses(ctx: AgentContext, max_dist: int = 3) -> list[ItemInfo]:
    """Corpses (graphic 0x2006) on the ground near the agent, nearest first."""
    ss = ctx.perception.self_state
    corpses = [
        it
        for it in ctx.perception.world.items.values()
        if (
            it.container == 0
            and it.graphic == CORPSE_GRAPHIC
            and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= max_dist
        )
    ]
    corpses.sort(key=lambda c: max(abs(c.x - ss.x), abs(c.y - ss.y)))
    return corpses


async def loot_corpse(ctx: AgentContext, corpse_serial: int) -> ActionResult:
    """Open a corpse and lift gold + valuables into the backpack.

    Sends DoubleClick on the corpse, waits for the server to stream the
    container contents (0x3C), then lifts gold (0x0EED) and
    vendor-sellable items one by one. Returns counts in ``data``
    (``items``, ``gold``).
    """
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(LAYER_BACKPACK)
    if not backpack:
        return ActionResult(success=False, message="No backpack")

    await ctx.conn.send_packet(build_double_click(corpse_serial))
    await asyncio.sleep(CONTENTS_WAIT_S)

    valuable = _valuable_graphics()
    items_picked = 0
    gold_picked = 0
    for it in list(ctx.perception.world.items.values()):
        if it.container != corpse_serial:
            continue
        is_gold = it.graphic == GOLD_GRAPHIC
        if not (is_gold or it.graphic in valuable):
            continue
        if ss.weight_max > 0 and ss.weight > ss.weight_max - WEIGHT_HEADROOM:
            break
        await ctx.conn.send_packet(build_pick_up(it.serial, it.amount))
        await asyncio.sleep(LIFT_DELAY_S)
        await ctx.conn.send_packet(build_drop_item(it.serial, container=backpack))
        await asyncio.sleep(LIFT_DELAY_S)
        items_picked += 1
        if is_gold:
            gold_picked += it.amount
        logger.info(
            "loot_picked",
            serial=f"0x{it.serial:08X}",
            graphic=f"0x{it.graphic:04X}",
            amount=it.amount,
            gold=is_gold,
        )

    return ActionResult(
        success=items_picked > 0,
        message=(
            f"Looted {items_picked} items ({gold_picked} gold)"
            if items_picked
            else "Corpse had nothing useful"
        ),
        data={"items": items_picked, "gold": gold_picked},
    )
