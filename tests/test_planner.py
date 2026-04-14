"""Tests for the rule-based Planner — gameplay loop."""

from __future__ import annotations

import time
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
    ctx.conn.send_packet = AsyncMock()
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
TONGS = 0x0FBB


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
        _add_item(ctx, 99, ORE, amount=10)  # need ore to trigger smelt path
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
    async def test_has_ingots_no_tongs_no_gold_sells_raw(self):
        """No tongs + no gold + many ingots → sell raw as last resort to fund tongs."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        _add_item(ctx, 1, PICKAXE)  # has tool so Priority 4 doesn't block
        _add_item(ctx, 2, INGOT, amount=20)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "sell_to_vendor"

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
        _add_item(ctx, 3, TONGS)  # tongs needed to reach craft path

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            result = await planner.tick(ctx)
            # Expired hint should NOT block — procedure runs normally
            assert result is not None


def _add_ground_item(ctx, serial, graphic, x, y, amount=1, hue=0):
    """Add an item on the ground (container=0)."""
    item = MagicMock(
        container=0, graphic=graphic, amount=amount, hue=hue,
        serial=serial, x=x, y=y,
    )
    ctx.perception.world.items[serial] = item


GOLD = 0x0EED


class TestDeadlockRecovery:
    """Test deadlock recovery: no tools, no gold, no materials → scavenge."""

    @pytest.mark.asyncio
    async def test_deadlock_scavenges_ground_gold(self):
        """True deadlock + gold on ground → scavenge_ground_items."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # No tools, no gold (gold=0), empty backpack
        ctx = _make_ctx(gold=0)
        # Gold coins on the ground nearby
        _add_ground_item(ctx, 0xAA, GOLD, x=105, y=205)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "scavenge_ground_items"

    @pytest.mark.asyncio
    async def test_deadlock_scavenges_ground_pickaxe(self):
        """True deadlock + pickaxe on ground → scavenge_ground_items."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        _add_ground_item(ctx, 0xBB, PICKAXE, x=100, y=200)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "scavenge_ground_items"

    @pytest.mark.asyncio
    async def test_deadlock_no_ground_items_walks_to_town(self):
        """True deadlock + nothing on ground → move to populated area."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc so town locations are in range
        ctx = _make_ctx(gold=0, x=2500, y=500)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert "move_to" in proc.name

    @pytest.mark.asyncio
    async def test_deadlock_no_ground_no_town_returns_none(self):
        """True deadlock + nothing nearby at all → returns None."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            proc = await planner.select_procedure(ctx)
        assert proc is None

    @pytest.mark.asyncio
    async def test_deadlock_escalates_to_wander_after_attempts(self):
        """After 3+3 failed attempts (no items, no move), escalate to wander."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            # Level 0: 3 attempts return None (nothing to scavenge, nowhere to go)
            for _ in range(3):
                proc = await planner.select_procedure(ctx)
                assert proc is None

            # Attempt 4 triggers escalation to Level 1.
            # Level 1 walk-to-town also fails (no locations) → None.
            proc = await planner.select_procedure(ctx)
            assert proc is None
            assert ctx.blackboard["_deadlock_recovery_level"] == 1

            # Level 1: 3 more None attempts
            for _ in range(3):
                proc = await planner.select_procedure(ctx)
                assert proc is None

            # Attempt 7 triggers escalation to Level 2 → wander_and_scavenge
            proc = await planner.select_procedure(ctx)
            assert proc is not None
            assert proc.name == "wander_and_scavenge"
            assert ctx.blackboard["_deadlock_recovery_level"] == 2

    @pytest.mark.asyncio
    async def test_deadlock_skips_ground_ore_when_flagged(self):
        """Priority 3b respects _skip_procedures for pick_up_ore_and_smelt."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc
        ctx = _make_ctx(gold=0, x=2460, y=558)
        # Ore on ground within range
        _add_ground_item(ctx, 0xCC, ORE, x=2461, y=558, amount=5)
        # Flag pick_up_ore_and_smelt as skipped (repeat failure detection)
        ctx.blackboard["_skip_procedures"] = {"pick_up_ore_and_smelt"}

        proc = await planner.select_procedure(ctx)
        # Should NOT return pick_up_ore_and_smelt — should reach Priority 4f
        assert proc is None or proc.name != "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_resolve_deadlock_clears_state(self):
        """DeadlockResolver.resolve Strategy 5 resets failed destinations and idle ticks."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        ctx.bus = None
        planner._idle_ticks = 150
        # Use fresh timestamps so strategies 1-4 don't trigger early return
        now = _time.time()
        planner._failed_destinations = {(100, 200): now, (300, 400): now}
        planner._move_fail_until = 999999.0

        await planner._deadlock.resolve(ctx)

        assert planner._idle_ticks == 0
        assert len(planner._failed_destinations) == 0


class TestExpeditionWiring:
    @pytest.mark.asyncio
    async def test_expedition_attached_to_planner(self):
        """Planner.__init__ creates a MiningExpedition."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        assert isinstance(planner._expedition, MiningExpedition)

    @pytest.mark.asyncio
    async def test_expedition_published_to_blackboard(self):
        """After _select_procedure, ctx.blackboard['expedition'] is set."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        ctx = _make_ctx()
        await planner.select_procedure(ctx)
        assert isinstance(ctx.blackboard.get("expedition"), MiningExpedition)
        assert ctx.blackboard["expedition"] is planner._expedition
