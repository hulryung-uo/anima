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
# Conservative per-lift weight estimate (stones). ss.weight only refreshes
# when the server echoes a 0x11 status update, which lags far behind the
# ~0.6s-per-item lift loop below. Without a local projection the gate reads
# a stale weight for the whole corpse and over-loots the agent into the
# overweight (movement-blocked) state. Gold is ~1 stone / 100 coins.
WEIGHT_PER_ITEM_EST = 2


def _valuable_graphics() -> set[int]:
    """Graphics worth lifting besides gold (vendor-sellable / tools).

    Lazy imports — these modules import action primitives themselves,
    so resolving them at call time avoids import cycles.

    Includes the agent's own combat gear (``WEAPON_GRAPHICS`` /
    ``SHIELD_GRAPHICS``): when a warrior dies, the weapon it was wielding
    drops into its corpse, and the post-resurrection recovery path
    (``planner._RecoverAfterDeath``) loots the corpse and then re-equips
    via ``equip_weapon_from_pack(ctx, WEAPON_GRAPHICS)``. Most weapon
    graphics (all mace-fighting weapons, plus several sword/fencing
    variants) are NOT in ``ITEM_VENDOR_MAP``, so without them here
    ``loot_corpse`` left the weapon rotting in the corpse, the backpack
    stayed empty, the re-equip found nothing, and the agent resumed the
    profession loop unarmed → immediate re-death.
    """
    from anima.actions.equip import SHIELD_GRAPHICS
    from anima.procedures.combat_loop import WEAPON_GRAPHICS
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
        | WEAPON_GRAPHICS
        | SHIELD_GRAPHICS
        | set(ITEM_VENDOR_MAP.keys())
    )


def _loot_weight_est(graphic: int, amount: int) -> int:
    """Conservative per-lift weight estimate (stones) for a corpse item.

    Gold is ~1 stone / 100 coins (floored at 1 so a tiny pile still costs
    something); every other valuable is a flat minimum. Pulled out of the
    lift loop so the pre-lift sort and the gate share one estimator.
    """
    if graphic == GOLD_GRAPHIC:
        return max(1, (amount + 99) // 100)
    return WEIGHT_PER_ITEM_EST


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
    (``items``, ``gold``, ``weight_gated``).
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
    # Local running weight projection — seeded from the last server-reported
    # weight and advanced per lift so the gate stops BEFORE the next stale
    # 0x11 status update would arrive.
    projected_weight = ss.weight
    # Lift order matters under the weight gate: once a projected lift would
    # cross the headroom band we ``break``, abandoning the rest of the
    # corpse. In raw ``world.items`` iteration order a heavy ore/ingot can
    # come BEFORE the gold pile, trip the gate, and strand the gold — the
    # single most valuable, near-weightless thing on the corpse and a direct
    # gold-rate loss. Sort gold first, then lightest-to-heaviest, so gold is
    # never sacrificed for a heavy item and the budget lifts the most items.
    candidates = [
        it
        for it in ctx.perception.world.items.values()
        if it.container == corpse_serial
        and (it.graphic == GOLD_GRAPHIC or it.graphic in valuable)
    ]
    candidates.sort(
        key=lambda it: (
            0 if it.graphic == GOLD_GRAPHIC else 1,
            _loot_weight_est(it.graphic, it.amount),
        )
    )
    # When the weight gate ``break``s the loop, valuables are still sitting in
    # the corpse — the lift was PARTIAL, not complete. The caller's per-corpse
    # dedup (``combat_loop._loot_fresh_corpses``) must NOT then retire the
    # corpse forever, or the stranded gold/items are lost the moment the pack
    # later frees up (sell/bank). Surface that distinction so the caller can
    # keep a weight-gated corpse eligible for a retry, while still retiring a
    # corpse that was emptied to completion.
    weight_gated = False
    for it in candidates:
        is_gold = it.graphic == GOLD_GRAPHIC
        est = _loot_weight_est(it.graphic, it.amount)
        # Gate on the PROJECTED post-lift weight (>=, so the headroom band
        # is honoured exactly) using the freshest server reading available.
        projected_weight = max(projected_weight, ss.weight)
        if (
            ss.weight_max > 0
            and projected_weight + est > ss.weight_max - WEIGHT_HEADROOM
        ):
            weight_gated = True
            break
        await ctx.conn.send_packet(build_pick_up(it.serial, it.amount))
        await asyncio.sleep(LIFT_DELAY_S)
        await ctx.conn.send_packet(build_drop_item(it.serial, container=backpack))
        await asyncio.sleep(LIFT_DELAY_S)
        projected_weight += est
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
        data={"items": items_picked, "gold": gold_picked, "weight_gated": weight_gated},
    )
