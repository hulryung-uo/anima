"""Integration test — one MINING → COLLECTING → CRAFTING_TRIP → MINING cycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.planner.expedition import Phase
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


def _ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 2460, 558, 5
    ss.hits, ss.hits_max = 100, 100
    ss.weight, ss.weight_max = 100, 400
    ss.gold = 0
    ss.equipment = {0x15: 0x101}
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "Grimm"
    return ctx


def _add(ctx, serial, graphic, amount=1):
    it = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=0, serial=serial,
    )
    ctx.perception.world.items[serial] = it


PICKAXE, ORE, INGOT, TONGS = 0x0E86, 0x19B9, 0x1BF2, 0x0FBB


@pytest.mark.asyncio
async def test_one_full_expedition_cycle():
    """
    Walk through the full state machine:
    1. IDLE — no procedure selected phase-wise, planner picks mine_ore via Priority 7
    2. Simulate note_ore_mined → phase = MINING, piles += 1
    3. Force scan_empty=True → MINING → COLLECTING
    4. Empty piles + enough ingots → COLLECTING → CRAFTING_TRIP
    5. Crafting done (no ingots, near home) → CRAFTING_TRIP → MINING, cycles += 1
    """
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))
    reg.register(_Stub("mine_ore"))
    reg.register(_Stub("smelt_ore"))
    reg.register(_Stub("craft_blacksmith"))
    reg.register(_Stub("sell_to_vendor"))
    planner = Planner(reg)
    ctx = _ctx()
    _add(ctx, 0xA0, PICKAXE, 1)

    # Stage 1: IDLE → select something that can mine
    proc1 = await planner.select_procedure(ctx)
    assert planner._expedition.phase in (Phase.IDLE, Phase.MINING)
    assert proc1 is not None

    # Stage 2: simulate a successful mine (the hook runs inside mine.py in prod)
    planner._expedition.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
    assert planner._expedition.phase == Phase.MINING
    assert len(planner._expedition.piles) == 1

    # Stage 3: no more banks → MINING → COLLECTING
    with patch.object(planner, "_scan_has_mineable_bank", return_value=False):
        await planner.select_procedure(ctx)
    assert planner._expedition.phase == Phase.COLLECTING

    # Stage 4: empty piles + 16 ingots → CRAFTING_TRIP
    planner._expedition.piles = []
    _add(ctx, 0xA1, INGOT, 16)
    _add(ctx, 0xA2, TONGS, 1)
    await planner.select_procedure(ctx)
    assert planner._expedition.phase == Phase.CRAFTING_TRIP

    # Stage 5: crafting done — no ingots, no crafted items, at home_base → MINING
    # Remove the ingot stack
    ctx.perception.world.items.pop(0xA1, None)
    ctx.perception.self_state.x, ctx.perception.self_state.y = 2460, 558
    start_cycles = planner._expedition.cycles_completed
    await planner.select_procedure(ctx)
    assert planner._expedition.phase == Phase.MINING
    assert planner._expedition.cycles_completed == start_cycles + 1
