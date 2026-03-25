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


class BankDeposit(Procedure):
    name = "bank_deposit"
    description = "Deposit gold and heavy items at the bank."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        has_gold = ss.gold >= GOLD_THRESHOLD
        has_heavy = False
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.6:
            backpack = ss.equipment.get(0x15)
            if backpack:
                has_heavy = any(
                    it.graphic in DEPOSIT_GRAPHICS
                    for it in world.items.values()
                    if it.container == backpack
                )

        if not has_gold and not has_heavy:
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

        # Deposit gold
        for item in list(world.items.values()):
            if item.container == backpack and item.graphic == GOLD_GRAPHIC:
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_count += 1

        # Deposit heavy materials if overweight
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.8:
            for item in list(world.items.values()):
                if (item.container == backpack
                        and item.graphic in DEPOSIT_GRAPHICS
                        and item.graphic not in KEEP_GRAPHICS):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.1)
                    await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                    await asyncio.sleep(0.2)
                    deposited_count += 1

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
