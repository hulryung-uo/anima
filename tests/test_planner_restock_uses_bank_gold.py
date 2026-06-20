"""Priority-4.5 regression: tool restock must count BANK gold, not just backpack.

Vendor purchases on this shard deduct from the BANK when the backpack is short
(Priority 4c and buy_from_vendor._available_funds both rely on this). After a
normal sell→bank leg the agent carries ~0 backpack gold but hundreds in the
bank. The old Priority-4.5 gate `ss.gold >= 10` is backpack-only, so it silently
skipped restock forever — a miner with one worn pickaxe and a full bank could
never top up to TOOL_MIN_STOCK. The fix gates on the same combined
backpack+fresh-bank funds check buy_from_vendor.can_start uses.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import Procedure, ProcedureRegistry, ProcedureResult


class _Stub(Procedure):
    def __init__(self, name: str, can: bool = True):
        self.name = name
        self.description = f"Stub {name}"
        self._can = can

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} done")


PICKAXE = 0x0E86  # one mining tool → passes Priority 4, mining_tool_count == 1


def _make_ctx(*, backpack_gold: int, bank_amount: int | None):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 100, 200, 0
    ss.hits = ss.hits_max = 100
    ss.weight = 100
    ss.weight_max = 400
    ss.gold = backpack_gold
    ss.equipment = {0x15: 0x101}
    ss.is_alive = True
    ss.is_poisoned = False
    ss.serial = 0x9999
    ss.skills = {}
    ctx.perception.world.items = {}
    ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])
    ctx.conn.send_packet = AsyncMock()
    ctx.conn.connected = True
    ctx.blackboard = {}
    if bank_amount is not None:
        # Fresh bank-balance cache, as check_bank_balance would write it.
        ctx.blackboard["bank_balance"] = {"amount": bank_amount, "ts": time.time()}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    ctx.persona.profession = ""
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    ctx.perception.world.items[serial] = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial,
    )


def _planner_with_buy():
    reg = ProcedureRegistry()
    reg.register(_Stub("buy_from_vendor", can=True))  # vendor reachable + affordable
    reg.register(_Stub("mine_ore", can=True))         # mining always available
    planner = Planner(reg)
    planner._roaming.move_to_location = AsyncMock(return_value=None)
    planner._roaming.try_move_to_activity = AsyncMock(return_value=None)
    return planner


@pytest.mark.asyncio
async def test_restock_with_empty_backpack_but_bank_gold():
    """0 backpack gold + fresh bank gold + 1 pickaxe → restock, don't fall to mining."""
    planner = _planner_with_buy()
    ctx = _make_ctx(backpack_gold=0, bank_amount=500)
    _add_item(ctx, 1, PICKAXE)  # exactly one tool → below TOOL_MIN_STOCK

    proc = await planner.select_procedure(ctx)
    assert proc is not None and proc.name == "buy_from_vendor", (
        "a miner with bank gold and one pickaxe must restock from bank funds, "
        f"got {None if proc is None else proc.name}"
    )


@pytest.mark.asyncio
async def test_restock_skipped_when_both_pockets_empty():
    """0 backpack gold + 0 bank gold → cannot afford, fall through to mining."""
    planner = _planner_with_buy()
    ctx = _make_ctx(backpack_gold=0, bank_amount=0)
    _add_item(ctx, 1, PICKAXE)

    proc = await planner.select_procedure(ctx)
    assert proc is not None and proc.name == "mine_ore", (
        f"truly broke agent should keep mining, not buy, got "
        f"{None if proc is None else proc.name}"
    )


@pytest.mark.asyncio
async def test_restock_with_backpack_gold_still_works():
    """Sanity: backpack gold alone still funds the restock (old happy path)."""
    planner = _planner_with_buy()
    ctx = _make_ctx(backpack_gold=50, bank_amount=None)
    _add_item(ctx, 1, PICKAXE)

    proc = await planner.select_procedure(ctx)
    assert proc is not None and proc.name == "buy_from_vendor", (
        f"backpack gold should still fund restock, got "
        f"{None if proc is None else proc.name}"
    )
