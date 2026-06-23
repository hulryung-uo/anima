"""Priority-5b regression: ``crafted_count`` must not be double-counted.

``crafted_count`` is computed once near the top of ``select_procedure`` (a full
backpack count of CRAFTED_ITEM_GRAPHICS). The Priority-5b sell block then walked
the backpack again to collect the distinct vendor sell-graphics — but it ALSO
did ``crafted_count += 1`` per item, re-adding the whole count on top of itself.

The result was a ``crafted_count`` of exactly 2x reality, which corrupted:
  * the ``planner_intent`` UI string ("제작품 N개 보유"),
  * the ``planner_sell_crafted`` telemetry the foundry eval / operators read,
  * the ``crafted_count`` value handed to ``should_return_to_mine`` for the
    CRAFTING_TRIP -> MINING cycle-completion gate.

This test pins the count surfaced to the rest of the planner to the true
backpack count, not double.
"""

from __future__ import annotations

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


PICKAXE = 0x0E86
DAGGER = 3921  # 0x0F51 — in CRAFTED_ITEM_GRAPHICS


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
    ctx.persona.profession = ""  # generic miner ladder — no adventurer loot-flush
    for k, v in overrides.items():
        setattr(ss, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    ctx.perception.world.items[serial] = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial,
    )


@pytest.mark.asyncio
async def test_crafted_count_not_doubled_in_intent():
    """Two crafted daggers must report as 2, not 4 (the double-count bug)."""
    reg = ProcedureRegistry()
    # A reachable vendor so Priority 5b returns the sell proc and stamps the
    # "제작품 N개 보유 → 상점에 판매" intent with the count under test.
    reg.register(_Stub("sell_to_vendor", can=True))
    planner = Planner(reg)

    ctx = _make_ctx()
    _add_item(ctx, 1, PICKAXE)            # has a mining tool → skip Priority 4
    _add_item(ctx, 2, DAGGER)             # crafted item #1
    _add_item(ctx, 3, DAGGER)             # crafted item #2

    proc = await planner.select_procedure(ctx)

    assert proc is not None and proc.name == "sell_to_vendor", (
        f"two crafted items should drive a vendor sell, got {proc!r}"
    )
    intent = ctx.blackboard.get("planner_intent", "")
    assert "2개" in intent, f"expected true count 2 in intent, got: {intent!r}"
    assert "4개" not in intent, f"crafted_count double-counted: {intent!r}"
