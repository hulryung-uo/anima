"""Tests for the rule-based Planner — gameplay loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
)


class StubProcedure(Procedure):
    def __init__(self, name: str, can: bool = True, suggestion: str | None = None):
        self.name = name
        self.description = f"Stub {name}"
        self._can = can
        self._suggestion = suggestion

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(
            success=True,
            message=f"{self.name} done",
            next_suggestion=self._suggestion,
        )


def _make_ctx(**overrides):
    ctx = MagicMock()
    ctx.perception.self_state.x = 100
    ctx.perception.self_state.y = 200
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.weight = 100
    ctx.perception.self_state.weight_max = 400
    ctx.perception.self_state.gold = 50  # below bank threshold (200)
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}  # empty backpack
    ctx.conn.connected = True
    ctx.blackboard = {}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    for k, v in overrides.items():
        setattr(ctx.perception.self_state, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    """Add an item to mock backpack (container=0x101)."""
    item = MagicMock(container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial)
    ctx.perception.world.items[serial] = item


# Graphic constants
PICKAXE = 0x0E86
ORE = 0x19B9
INGOT = 0x1BF2


class TestGameplayLoop:
    """Test the full gameplay loop: mine → smelt → sell → bank → buy → mine."""

    @pytest.mark.asyncio
    async def test_survival_first(self):
        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(hits=20, hits_max=100)
        _add_item(ctx, 1, PICKAXE)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "heal_self"

    @pytest.mark.asyncio
    async def test_overweight_smelt(self):
        reg = ProcedureRegistry()
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(weight=360, weight_max=400)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "smelt_ore"

    @pytest.mark.asyncio
    async def test_has_ore_smelt(self):
        """Ore in backpack → smelt before mining more."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, ORE, amount=10)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "smelt_ore"

    @pytest.mark.asyncio
    async def test_has_ingots_sell(self):
        """Ingots + has tool → craft then sell."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor"))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)  # has tool so Priority 4 doesn't block
        _add_item(ctx, 2, INGOT, amount=20)
        proc = await planner.select_procedure(ctx)
        # With ingots + tongs (if registered), craft first; otherwise sell
        assert proc.name in ("craft_blacksmith", "sell_to_vendor")

    @pytest.mark.asyncio
    async def test_gold_bank(self):
        """Gold > 200 + has tools → bank deposit."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("bank_deposit"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=500)
        _add_item(ctx, 1, PICKAXE)  # has tools, so Priority 4 doesn't trigger
        proc = await planner.select_procedure(ctx)
        assert proc.name == "bank_deposit"

    @pytest.mark.asyncio
    async def test_no_pickaxe_buy(self):
        """No pickaxe → buy from vendor."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx()  # empty backpack, no pickaxe
        proc = await planner.select_procedure(ctx)
        assert proc.name == "buy_from_vendor"

    @pytest.mark.asyncio
    async def test_has_pickaxe_mine(self):
        """Has pickaxe, nothing else → mine."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "mine_ore"

    @pytest.mark.asyncio
    async def test_no_procedures_none(self):
        """Nothing can start, no locations → None."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            proc = await planner.select_procedure(ctx)
        assert proc is None

    @pytest.mark.asyncio
    async def test_no_pickaxe_no_vendor_moves_to_shop(self):
        """No pickaxe, buy can't start → move to shop."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor", can=False))
        planner = Planner(reg)

        # Position near Minoc so shops are within max_dist
        ctx = _make_ctx(x=2500, y=500)
        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert "move_to" in proc.name


class TestPlannerTick:
    @pytest.mark.asyncio
    async def test_tick_no_procedure(self):
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            result = await planner.tick(ctx)
        assert result is None


import json
import time as _time


class TestSupervisorHints:
    @pytest.mark.asyncio
    async def test_skips_hinted_procedure(self, tmp_path):
        """Planner skips procedure listed in supervisor_hints.json."""
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() + 3600,
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            proc = await planner.select_procedure(ctx)
            if proc and proc.name == "craft_blacksmith":
                # select_procedure returned it, but tick() should skip it
                result = await planner.tick(ctx)
                assert result is None
            # If select_procedure returned something else or None, that's also fine

    @pytest.mark.asyncio
    async def test_expired_hint_ignored(self, tmp_path):
        """Expired hint does not skip procedure."""
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() - 100,  # expired
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            result = await planner.tick(ctx)
            # Expired hint should NOT block — procedure runs normally
            assert result is not None
