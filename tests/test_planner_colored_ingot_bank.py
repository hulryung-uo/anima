"""Priority-6 regression: a stray colored-ingot stack must not idle a miner.

Colored (non-iron) ore spawns at random hues during ordinary mining, so a
carried colored-ingot stack is a passive byproduct, not a goal. Priority 6
routes such ingots to the bank, but when no bank is reachable the move helper
returns None — and the old code returned that None straight up, sending an
otherwise-healthy, tool-equipped miner idle for the whole bank cooldown.
The fix: when colored ingots are the ONLY reason we entered Priority 6 and the
bank is unreachable, fall through to the mining loop (Priority 7) and keep
earning. A genuinely large gold pile (>200) still waits for the bank.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import Procedure, ProcedureResult
from anima.procedures.base import ProcedureRegistry


class _Stub(Procedure):
    def __init__(self, name: str, can: bool = True):
        self.name = name
        self.description = f"Stub {name}"
        self._can = can

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} done")


PICKAXE = 0x0E86
COLORED_INGOT = 0x1BF2  # in INGOT_GRAPHICS; non-zero hue → colored


def _make_ctx(**overrides):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = 100
    ss.y = 200
    ss.z = 0
    ss.hits = 100
    ss.hits_max = 100
    ss.weight = 100
    ss.weight_max = 400
    ss.gold = 50  # below the 200 bank threshold
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
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    ctx.persona.profession = ""
    for k, v in overrides.items():
        setattr(ss, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    ctx.perception.world.items[serial] = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial,
    )


@pytest.mark.asyncio
async def test_colored_ingots_bank_unreachable_falls_through_to_mining():
    """Colored ingots + no reachable bank + pickaxe → keep mining, not idle."""
    reg = ProcedureRegistry()
    reg.register(_Stub("bank_deposit", can=False))  # bank not in range
    reg.register(_Stub("mine_ore", can=True))       # mining is available
    planner = Planner(reg)
    # Bank is unreachable from this position.
    planner._roaming.move_to_location = AsyncMock(return_value=None)

    ctx = _make_ctx(gold=50)
    _add_item(ctx, 1, PICKAXE)
    _add_item(ctx, 2, COLORED_INGOT, amount=10, hue=0x973)  # DullCopper

    proc = await planner.select_procedure(ctx)
    assert proc is not None, "must not idle a tool-equipped miner over a bank trip"
    assert proc.name == "mine_ore", (
        f"expected fall-through to mining when bank unreachable, got {proc.name}"
    )


@pytest.mark.asyncio
async def test_large_gold_pile_still_waits_for_unreachable_bank():
    """A real gold hoard (>200) has no in-pack use → still wait for the bank."""
    reg = ProcedureRegistry()
    reg.register(_Stub("bank_deposit", can=False))
    reg.register(_Stub("mine_ore", can=True))
    planner = Planner(reg)
    planner._roaming.move_to_location = AsyncMock(return_value=None)

    ctx = _make_ctx(gold=500)
    _add_item(ctx, 1, PICKAXE)

    proc = await planner.select_procedure(ctx)
    assert proc is None, "large gold pile should keep waiting for a bank, not mine"


@pytest.mark.asyncio
async def test_colored_ingots_bank_reachable_still_goes_to_bank():
    """Sanity: when a bank IS reachable, colored ingots still drive the trip."""
    reg = ProcedureRegistry()
    reg.register(_Stub("bank_deposit", can=False))
    reg.register(_Stub("mine_ore", can=True))
    planner = Planner(reg)
    sentinel = _Stub("move_to_bank")
    planner._roaming.move_to_location = AsyncMock(return_value=sentinel)

    ctx = _make_ctx(gold=50)
    _add_item(ctx, 1, PICKAXE)
    _add_item(ctx, 2, COLORED_INGOT, amount=10, hue=0x973)

    proc = await planner.select_procedure(ctx)
    assert proc is sentinel, "reachable bank → take the bank trip"
