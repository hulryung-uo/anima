"""SellToVendor must not report success when gold never moved.

ServUO's OnVendorSell silently drops the transaction when the request is
rejected (out of range, vendor window closed, etc.) — the sell packet is sent
but the agent's gold does not change. Previously SellToVendor logged a warning
and STILL returned success=True, writing a phantom win to the ActionLog reward
signal and stopping the planner from retrying. A rejected sell with positive
expected value must surface as a retryable BLOCKED failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.self_state import VendorSellItem
from anima.procedures.base import FailureReason
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
    # A priced, non-KEEP item the vendor offers to buy.
    ctx.perception.self_state.vendor_sell_list = [
        VendorSellItem(serial=0x900, graphic=0xCCCC, amount=2, price=12, name="loot")
    ]
    ctx.perception.self_state.vendor_serial = 0
    ctx.perception.self_state.context_menu = []
    ctx.perception.self_state.context_menu_serial = 0
    ctx.perception.self_state.equipment = {}
    ctx.perception.world.items = {}
    ctx.blackboard = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestSeller"
    return ctx


def _vendor():
    v = MagicMock()
    v.serial = 0x5C1
    v.name = "Belulah the provisioner"
    v.x = 1401
    v.y = 1681
    return v


@pytest.mark.asyncio
async def test_sell_with_no_gold_change_is_blocked_failure():
    """Gold unchanged after a positive-value sell -> retryable BLOCKED, not success."""
    proc = SellToVendor()
    ctx = _make_ctx()

    with patch(
        "anima.procedures.sell_to_vendor._find_vendor", return_value=_vendor()
    ), patch(
        "anima.procedures.sell_to_vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ), patch(
        # gold stays at 500 — the server rejected the sell
        "anima.skills.trade.vendor._wait_for_gold_change",
        AsyncMock(return_value=None),
    ):
        result = await proc.execute(ctx)

    assert result.success is False
    assert result.reason == FailureReason.BLOCKED
    assert result.gold_changed == 0
    assert result.details["expected_gold"] > 0


@pytest.mark.asyncio
async def test_sell_with_gold_change_is_success():
    """Control: when gold actually increases, the sell is a success."""
    proc = SellToVendor()
    ctx = _make_ctx()

    async def _bump_gold(ss, before, timeout):
        ss.gold = before + 24  # 2 x 12gp

    with patch(
        "anima.procedures.sell_to_vendor._find_vendor", return_value=_vendor()
    ), patch(
        "anima.procedures.sell_to_vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ), patch(
        "anima.skills.trade.vendor._wait_for_gold_change",
        AsyncMock(side_effect=_bump_gold),
    ):
        result = await proc.execute(ctx)

    assert result.success is True
    assert result.gold_changed == 24
