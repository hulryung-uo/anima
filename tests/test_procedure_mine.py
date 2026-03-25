"""Tests for MineOre procedure (Step 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.base import FailureReason, ProcedureResult
from anima.procedures.mine_ore import MineOre


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 2500
    ctx.perception.self_state.y = 550
    ctx.perception.self_state.z = 15
    ctx.perception.self_state.serial = 0x100
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.pending_target = None
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestMiner"
    return ctx


class TestMineOreCanStart:
    @pytest.mark.asyncio
    async def test_no_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        # No items in backpack
        assert not await proc.can_start(ctx)

    @pytest.mark.asyncio
    async def test_has_pickaxe_no_tile(self):
        proc = MineOre()
        ctx = _make_ctx()
        # Add pickaxe to backpack
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}
        # No map reader → no tiles
        ctx.map_reader = None
        # _find_mineable_tile checks map statics, will return None
        assert not await proc.can_start(ctx)

    @pytest.mark.asyncio
    async def test_diagnose_no_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        reason = await proc.diagnose(ctx)
        assert reason is not None
        assert "pickaxe" in reason


class TestMineOreExecute:
    @pytest.mark.asyncio
    async def test_missing_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        result = await proc.execute(ctx)
        assert not result.success
        assert result.reason == FailureReason.MISSING_RESOURCE

    @pytest.mark.asyncio
    async def test_interrupted_by_low_hp(self):
        proc = MineOre()
        ctx = _make_ctx()
        # Add pickaxe
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}

        # Set low HP
        ctx.perception.self_state.hits = 20
        ctx.perception.self_state.hits_max = 100

        # Mock _find_mineable_tile to return a tile
        with patch("anima.procedures.mine_ore._find_mineable_tile") as mock_find:
            mock_find.return_value = (2501, 540, 15, 220, False)
            result = await proc.execute(ctx)

        assert not result.success
        assert result.reason == FailureReason.INTERRUPTED

    @pytest.mark.asyncio
    async def test_no_tile(self):
        proc = MineOre()
        ctx = _make_ctx()
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}
        ctx.perception.self_state.hits = 100
        ctx.perception.self_state.hits_max = 100

        with patch("anima.procedures.mine_ore._find_mineable_tile") as mock_find:
            mock_find.return_value = None
            result = await proc.execute(ctx)

        assert not result.success
        assert result.reason == FailureReason.WRONG_LOCATION


class TestMineOreRun:
    @pytest.mark.asyncio
    async def test_run_wraps_execute(self):
        """run() should wrap execute() with timing and ActionLog."""
        proc = MineOre()
        ctx = _make_ctx()
        result = await proc.run(ctx)
        # Should fail gracefully (no pickaxe)
        assert not result.success
        assert result.reason == FailureReason.MISSING_RESOURCE
