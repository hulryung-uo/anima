"""Tests for Buy/Sell vendor procedures (Step 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.base import FailureReason
from anima.procedures.buy_from_vendor import BuyFromVendor
from anima.procedures.sell_to_vendor import SellToVendor


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 1400
    ctx.perception.self_state.y = 1680
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.serial = 0x100
    ctx.perception.self_state.gold = 500
    ctx.perception.self_state.weight = 100
    ctx.perception.self_state.weight_max = 400
    ctx.perception.self_state.vendor_buy_list = None
    ctx.perception.self_state.vendor_sell_list = None
    ctx.perception.self_state.context_menu = []
    ctx.perception.self_state.context_menu_serial = 0
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestBuyer"
    return ctx


class TestBuyFromVendor:
    @pytest.mark.asyncio
    async def test_no_gold(self):
        proc = BuyFromVendor()
        ctx = _make_ctx()
        ctx.perception.self_state.gold = 5
        with patch("anima.procedures.buy_from_vendor._find_vendor", return_value=MagicMock()):
            result = await proc.can_start(ctx)
        assert not result

    @pytest.mark.asyncio
    async def test_no_vendor(self):
        proc = BuyFromVendor()
        ctx = _make_ctx()
        with patch("anima.procedures.buy_from_vendor._find_vendor", return_value=None):
            result = await proc.can_start(ctx)
        assert not result

    @pytest.mark.asyncio
    async def test_overweight_rejects(self):
        """Overweight agent should not attempt to buy — server rejects silently."""
        proc = BuyFromVendor()
        ctx = _make_ctx()
        ctx.perception.self_state.weight = 400  # 91% of 439
        ctx.perception.self_state.weight_max = 439
        with patch("anima.procedures.buy_from_vendor._find_vendor", return_value=MagicMock()):
            result = await proc.can_start(ctx)
        assert not result

    @pytest.mark.asyncio
    async def test_execute_no_vendor(self):
        proc = BuyFromVendor()
        ctx = _make_ctx()
        with patch("anima.procedures.buy_from_vendor._find_vendor", return_value=None):
            result = await proc.execute(ctx)
        assert not result.success
        assert result.reason == FailureReason.WRONG_LOCATION


class TestSellToVendor:
    @pytest.mark.asyncio
    async def test_no_vendor(self):
        proc = SellToVendor()
        ctx = _make_ctx()
        with patch("anima.procedures.sell_to_vendor._find_vendor", return_value=None):
            result = await proc.can_start(ctx)
        assert not result

    @pytest.mark.asyncio
    async def test_execute_no_vendor(self):
        proc = SellToVendor()
        ctx = _make_ctx()
        with patch("anima.procedures.sell_to_vendor._find_vendor", return_value=None):
            result = await proc.execute(ctx)
        assert not result.success
        assert result.reason == FailureReason.WRONG_LOCATION
