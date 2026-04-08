"""BankDeposit procedure — deposit gold and items at the bank."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_double_click,
    build_drop_item,
    build_pick_up,
    build_unicode_speech,
)
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.banking import (
    DEPOSIT_GRAPHICS,
    GOLD_GRAPHIC,
    GOLD_THRESHOLD,
    KEEP_GRAPHICS,
    _find_banker,
    _wait_for_bank_box,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


# Ingot graphic — all ingot types share this; only the hue differs.
# Iron ingots use hue 0; everything else (DullCopper 0x973, ShadowIron 0x966,
# Copper 0x96D, Bronze 0x972, Gold 0x8A5, Agapite 0x979, Verite 0x89F,
# Valorite 0x8AB) is a colored ingot. Source: ServUO Misc/ResourceInfo.cs.
INGOT_GRAPHIC = 0x1BF2
IRON_HUE = 0


def _is_colored_ingot(item) -> bool:
    """True if this item is an ingot with a non-iron hue."""
    return item.graphic == INGOT_GRAPHIC and item.hue != IRON_HUE


def _has_colored_ingots(ctx: AgentContext) -> bool:
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return False
    return any(
        _is_colored_ingot(it)
        for it in ctx.perception.world.items.values()
        if it.container == backpack
    )


class BankDeposit(Procedure):
    name = "bank_deposit"
    description = "Deposit gold and heavy items at the bank."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        has_gold = ss.gold >= GOLD_THRESHOLD

        # Always bank colored ingots — they can't be used for the basic
        # iron-based crafting recipes and would otherwise pile up forever.
        has_colored = _has_colored_ingots(ctx)

        has_heavy = False
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.6:
            backpack = ss.equipment.get(0x15)
            if backpack:
                has_heavy = any(
                    it.graphic in DEPOSIT_GRAPHICS
                    for it in world.items.values()
                    if it.container == backpack
                )

        if not has_gold and not has_heavy and not has_colored:
            return False

        return _find_banker(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        banker = _find_banker(ctx)
        if not banker:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no banker nearby",
            )

        gold_before = ss.gold

        # Open bank: say "bank" + double-click banker
        ss.gumps.clear()
        ss.open_container = 0
        await ctx.conn.send_packet(build_unicode_speech("bank"))
        await asyncio.sleep(0.3)
        await ctx.conn.send_packet(build_double_click(banker.serial))

        bank_serial = await _wait_for_bank_box(ctx)
        if not bank_serial:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="bank box did not open",
            )

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        deposited_count = 0
        colored_ingots_deposited = 0

        # Deposit gold
        for item in list(world.items.values()):
            if item.container == backpack and item.graphic == GOLD_GRAPHIC:
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_count += 1

        # Always deposit colored (non-iron) ingots — they're useless for
        # basic crafting and would otherwise accumulate indefinitely.
        for item in list(world.items.values()):
            if item.container == backpack and _is_colored_ingot(item):
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_count += 1
                colored_ingots_deposited += item.amount

        # Deposit heavy materials if overweight (skip iron ingots — needed
        # for crafting; colored ingots already handled above)
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.8:
            for item in list(world.items.values()):
                if (item.container == backpack
                        and item.graphic in DEPOSIT_GRAPHICS
                        and item.graphic not in KEEP_GRAPHICS
                        and not (item.graphic == INGOT_GRAPHIC and item.hue == IRON_HUE)):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.1)
                    await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                    await asyncio.sleep(0.2)
                    deposited_count += 1

        if colored_ingots_deposited:
            logger.info(
                "bank_deposited_colored_ingots",
                amount=colored_ingots_deposited,
            )

        await asyncio.sleep(0.5)

        if deposited_count > 0:
            actual_deposited = max(0, gold_before - ss.gold)
            return ProcedureResult(
                success=True,
                message=f"Deposited {actual_deposited}gp, {deposited_count} items",
                gold_changed=-actual_deposited,
                details={"items": deposited_count, "gold": actual_deposited},
            )
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="nothing to deposit",
            )
