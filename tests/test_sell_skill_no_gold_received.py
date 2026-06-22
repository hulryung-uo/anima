"""SellToNpc (skills path) must not report success when gold never moved.

ServUO's OnVendorSell silently drops the transaction when the request is
rejected (out of range, vendor window closed, etc.): the 0x9F sell packet is
sent but the agent's gold does not change and the items are NOT sold. The
skills-path SellToNpc previously logged a warning and STILL returned
success=True with a +0.5 reward, writing a phantom win into the Q/location
-value reward signal and telling the planner the sale landed so it stopped
retrying. A rejected sell with positive expected value must surface as a
negative-reward failure — mirroring the already-fixed procedures twin
(test_sell_no_gold_received.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.self_state import VendorSellItem
from anima.skills.trade.vendor import SellToNpc


def _make_ctx(gold: int = 500):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = 1400
    ss.y = 1680
    ss.z = 0
    ss.serial = 0x100
    ss.gold = gold
    ss.weight = 300
    ss.weight_max = 400
    ss.equipment = {0x15: 0x40}  # backpack serial
    # One priced, non-KEEP item the vendor offers to buy.
    ss.vendor_sell_list = [
        VendorSellItem(serial=0x900, graphic=0xCCCC, amount=2, price=12, name="loot")
    ]
    ss.vendor_serial = 0x5C1
    ss.context_menu = []
    ss.context_menu_serial = 0
    ctx.perception.world.items = {}
    ctx.blackboard = {}
    ctx.conn.send_packet = AsyncMock()
    return ctx


def _vendor():
    v = MagicMock()
    v.serial = 0x5C1
    v.name = "Belulah the provisioner"
    v.x = 1401
    v.y = 1681
    return v


@pytest.mark.asyncio
async def test_sell_skill_with_no_gold_change_is_failure():
    """Gold unchanged after a positive-value sell -> failure with negative reward."""
    skill = SellToNpc()
    ctx = _make_ctx(gold=500)

    sell_list = list(ctx.perception.self_state.vendor_sell_list)

    async def _populate_sell_list(_ctx, *_a, **_k):
        # execute() clears vendor_sell_list before waiting; the real
        # _wait_for_sell_list returns once the 0x9E handler refills it.
        ctx.perception.self_state.vendor_sell_list = list(sell_list)
        return True

    with patch(
        "anima.skills.trade.vendor._find_vendor_async",
        AsyncMock(return_value=_vendor()),
    ), patch(
        "anima.skills.trade.vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ), patch(
        "anima.skills.trade.vendor._wait_for_sell_list",
        AsyncMock(side_effect=_populate_sell_list),
    ), patch(
        "anima.actions.inventory.find_in_backpack", return_value=True,
    ), patch(
        "anima.skills.trade.vendor.asyncio.sleep", AsyncMock(),
    ):
        # gold stays at 500 across the whole transaction — server rejected it
        result = await skill.execute(ctx)

    assert result.success is False
    assert result.reward < 0
    assert "did not change" in result.message


@pytest.mark.asyncio
async def test_sell_skill_with_gold_change_is_success():
    """Control: when gold actually increases, the sell is a success."""
    skill = SellToNpc()
    ctx = _make_ctx(gold=500)

    sell_list = list(ctx.perception.self_state.vendor_sell_list)

    async def _populate_sell_list(_ctx, *_a, **_k):
        ctx.perception.self_state.vendor_sell_list = list(sell_list)
        return True

    async def _bump_gold(*_args, **_kwargs):
        # The sell landed — credit 24gp (2 x 12gp) when the packet is sent.
        ctx.perception.self_state.gold = 524

    with patch(
        "anima.skills.trade.vendor._find_vendor_async",
        AsyncMock(return_value=_vendor()),
    ), patch(
        "anima.skills.trade.vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ), patch(
        "anima.skills.trade.vendor._wait_for_sell_list",
        AsyncMock(side_effect=_populate_sell_list),
    ), patch(
        "anima.actions.inventory.find_in_backpack", return_value=True,
    ), patch(
        "anima.skills.trade.vendor.asyncio.sleep", AsyncMock(side_effect=_bump_gold),
    ):
        result = await skill.execute(ctx)

    assert result.success is True
    assert result.reward > 0
