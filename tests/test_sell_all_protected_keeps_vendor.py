"""SellToVendor must NOT blacklist a willing vendor when the only reason
nothing sold is our own KEEP_GRAPHICS protection.

The refused-vendor latch (_VENDOR_REFUSE_COOLDOWN, 300s) is for vendors that
genuinely won't trade with us — a "not interested" rejection or a vendor that
never answered the Sell request. When the vendor cooperates (sends a complete
0x9E sell list) and the ONLY reason items_to_sell is empty is that every
offered item is in KEEP_GRAPHICS (something WE chose to keep), marking the
vendor refused strands the next run: once the agent accumulates a sellable
item it would skip this nearest, perfectly-good vendor for the whole cooldown.

Contrast with a real rejection, which MUST still latch the vendor.
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
    # High gold so _dynamic_keep never relaxes ingot protection.
    ctx.perception.self_state.gold = 5000
    ctx.perception.self_state.weight = 100
    ctx.perception.self_state.weight_max = 400
    ctx.perception.self_state.vendor_buy_list = None
    # The vendor offers to buy ONLY a bandage stack — 0x0E21 is in
    # KEEP_GRAPHICS and belongs to no surplus-tool group, so the keep-at-
    # least-1 logic never relaxes it: items_to_sell ends up empty even though
    # the vendor cooperated and sent a full sell list.
    ctx.perception.self_state.vendor_sell_list = [
        VendorSellItem(serial=0x901, graphic=0x0E21, amount=10, price=3, name="bandages")
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
async def test_all_protected_does_not_blacklist_vendor():
    """Everything in the sell list is KEEP-protected -> BLOCKED, vendor still
    selectable (NOT added to refused_vendors)."""
    proc = SellToVendor()
    ctx = _make_ctx()
    vendor = _vendor()

    with patch(
        "anima.procedures.sell_to_vendor._find_vendor", return_value=vendor
    ), patch(
        "anima.procedures.sell_to_vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ):
        result = await proc.execute(ctx)

    assert result.success is False
    assert result.reason == FailureReason.BLOCKED
    # The willing vendor must NOT be latched into the refused set.
    refused = ctx.blackboard.get("refused_vendors", {})
    assert vendor.serial not in refused, (
        "a vendor that cooperated must not be blacklisted just because "
        "our own KEEP_GRAPHICS protected every offered item"
    )


@pytest.mark.asyncio
async def test_real_rejection_still_blacklists_vendor():
    """Control: a genuine 'not interested' rejection still latches the vendor."""
    proc = SellToVendor()
    ctx = _make_ctx()
    ctx.perception.self_state.vendor_sell_list = []
    vendor = _vendor()

    class _Bus:
        def __init__(self):
            self._cbs = []

        def subscribe(self, _topic, cb):
            self._cbs.append(cb)
            # Deliver the rejection synchronously so execute() sees it.
            cb("avatar.speech_heard", {"text": "I am not interested in that."})
            return object()

        def unsubscribe(self, _sub):
            pass

    ctx.bus = _Bus()

    with patch(
        "anima.procedures.sell_to_vendor._find_vendor", return_value=vendor
    ), patch(
        "anima.procedures.sell_to_vendor._request_context_menu_entry",
        AsyncMock(return_value=True),
    ):
        result = await proc.execute(ctx)

    assert result.success is False
    refused = ctx.blackboard.get("refused_vendors", {})
    assert vendor.serial in refused, (
        "a vendor that explicitly refuses our items must still be blacklisted"
    )
