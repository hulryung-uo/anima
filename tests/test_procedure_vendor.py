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
    ctx.perception.self_state.equipment = {}
    ctx.perception.world.items = {}
    ctx.blackboard = {}
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
    async def test_empty_pouch_fresh_bank_allows_buy(self):
        """Backpack=0 with a fresh bank balance ≥ 10 must still start.

        Regression guard: the shard pays vendor purchases from bank gold
        when the pouch can't cover, so an empty pouch alone is not a
        blocker. Previously can_start gated on ss.gold < 10 and
        short-circuited this path — the agent got pinned in deadlock
        recovery with 25k in the bank.
        """
        import time
        proc = BuyFromVendor()
        ctx = _make_ctx()
        ctx.perception.self_state.gold = 0
        ctx.blackboard["bank_balance"] = {"amount": 500, "ts": time.time()}
        with patch(
            "anima.procedures.buy_from_vendor._find_vendor",
            return_value=MagicMock(),
        ):
            assert await proc.can_start(ctx)

    @pytest.mark.asyncio
    async def test_empty_pouch_stale_bank_rejects(self):
        """Backpack=0 with only a stale bank reading must NOT start."""
        import time
        proc = BuyFromVendor()
        ctx = _make_ctx()
        ctx.perception.self_state.gold = 0
        ctx.blackboard["bank_balance"] = {"amount": 500, "ts": time.time() - 3600}
        with patch(
            "anima.procedures.buy_from_vendor._find_vendor",
            return_value=MagicMock(),
        ):
            assert not await proc.can_start(ctx)

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

    @pytest.mark.asyncio
    async def test_execute_no_buy_menu_marks_refused(self):
        """Vendor without a Buy context menu entry (e.g. miner guildmistress)
        must be marked refused so the planner stops retrying every tick."""
        proc = BuyFromVendor()
        ctx = _make_ctx()
        vendor = MagicMock()
        vendor.serial = 0x5C1
        vendor.name = " Ambar the miner guildmistress"
        vendor.x = 2460
        vendor.y = 487
        with patch(
            "anima.procedures.buy_from_vendor._find_vendor", return_value=vendor,
        ), patch(
            "anima.procedures.buy_from_vendor._request_context_menu_entry",
            AsyncMock(return_value=False),
        ), patch(
            "anima.procedures.buy_from_vendor._mark_refused",
        ) as mark_refused:
            result = await proc.execute(ctx)

        assert not result.success
        assert result.reason == FailureReason.BLOCKED
        mark_refused.assert_called_once_with(ctx, 0x5C1)


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
