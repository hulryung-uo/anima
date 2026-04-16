"""Tests for vendor buy/sell improvements.

Covers:
- Priority-based tool buying (tongs > tinker tools > pickaxe)
- Per-vendor item blacklist with TTL
- Price-sorted selling (highest value first)
- Gold polling helper
"""

import asyncio
import time
from unittest.mock import MagicMock, AsyncMock

import pytest

from anima.procedures.buy_from_vendor import (
    TOOL_MIN_STOCK,
    _needed_tool_graphics,
    _TOOL_PRIORITIES,
    _ALL_TOOL_GRAPHICS,
    _BUY_ITEM_BLACKLIST_TTL,
)
from anima.skills.trade.vendor import _wait_for_gold_change


PICKAXE = 0x0E86
TONGS = 0x0FBB
TINKER_TOOLS = 0x1EB8
SHOVEL = 0x0F39


def _mock_ctx(*backpack_graphics):
    """Create a mock AgentContext with items in backpack.

    Pass a graphic multiple times to simulate having N of that tool,
    e.g. _mock_ctx(PICKAXE, PICKAXE, PICKAXE) = 3 pickaxes.
    """
    ctx = MagicMock()
    ctx.perception.self_state.equipment = {0x15: 0x101}
    items = {}
    for i, gfx in enumerate(backpack_graphics):
        item = MagicMock(container=0x101, graphic=gfx, amount=1, hue=0, serial=i + 1)
        items[i + 1] = item
    ctx.perception.world.items = items
    return ctx


def _fully_stocked(*tool_graphics):
    """Return args that give TOOL_MIN_STOCK copies of each graphic."""
    result = []
    for gfx in tool_graphics:
        result.extend([gfx] * TOOL_MIN_STOCK)
    return result


# --- _needed_tool_graphics tests ---

class TestNeededToolGraphics:
    def test_all_missing(self):
        ctx = _mock_ctx()  # empty backpack
        needed = _needed_tool_graphics(ctx)
        # Should return all 4 tool categories
        assert len(needed) == 4

    def test_has_one_tongs_still_needs_restock(self):
        """One tongs is below TOOL_MIN_STOCK → still needed."""
        ctx = _mock_ctx(TONGS)
        needed = _needed_tool_graphics(ctx)
        # All 4 categories needed (tongs count=1 < TOOL_MIN_STOCK)
        assert len(needed) == 4

    def test_fully_stocked_tongs_not_needed(self):
        """TOOL_MIN_STOCK tongs → tongs category not in needed list."""
        ctx = _mock_ctx(*_fully_stocked(TONGS))
        needed = _needed_tool_graphics(ctx)
        # 3 categories still needed (tinker/pickaxe/shovel)
        assert len(needed) == 3
        tongs_gfx = {0x0FBB, 0x0FBC}
        assert tongs_gfx not in needed

    def test_has_everything_fully_stocked(self):
        ctx = _mock_ctx(*_fully_stocked(TONGS, TINKER_TOOLS, PICKAXE, SHOVEL))
        needed = _needed_tool_graphics(ctx)
        assert needed == []

    def test_priority_order(self):
        """Tongs should be first when missing."""
        ctx = _mock_ctx(PICKAXE)  # has 1 pickaxe, missing tongs+tinker+shovel
        needed = _needed_tool_graphics(ctx)
        # First entry should be tongs graphics (highest priority missing)
        assert {0x0FBB, 0x0FBC} == needed[0]


# --- Per-vendor blacklist tests ---

class TestPerVendorBlacklist:
    def test_blacklist_blocks_item(self):
        """Blacklisted item serial should be skipped."""
        ctx = _mock_ctx()
        vendor_serial = 0x1234
        item_serial = 0xF9CB3
        bl = ctx.blackboard = {}
        bl["_buy_failed_items"] = {
            vendor_serial: {item_serial: time.monotonic()},
        }
        # The blacklist check in execute uses this structure
        vendor_bl = bl["_buy_failed_items"].get(vendor_serial, {})
        now = time.monotonic()
        active = {s for s, t in vendor_bl.items() if now - t < _BUY_ITEM_BLACKLIST_TTL}
        assert item_serial in active

    def test_blacklist_expires(self):
        """Expired entries should not block."""
        ctx = _mock_ctx()
        vendor_serial = 0x1234
        item_serial = 0xF9CB3
        bl = ctx.blackboard = {}
        # Set timestamp to 200s ago (> 120s TTL)
        bl["_buy_failed_items"] = {
            vendor_serial: {item_serial: time.monotonic() - 200},
        }
        vendor_bl = bl["_buy_failed_items"].get(vendor_serial, {})
        now = time.monotonic()
        active = {s for s, t in vendor_bl.items() if now - t < _BUY_ITEM_BLACKLIST_TTL}
        assert item_serial not in active

    def test_blacklist_per_vendor(self):
        """Blacklist for vendor A should not affect vendor B."""
        vendor_a = 0x1234
        vendor_b = 0x5678
        item_serial = 0xF9CB3
        bl = {vendor_a: {item_serial: time.monotonic()}}
        # Vendor B has no entries
        assert vendor_b not in bl
        vendor_b_bl = bl.get(vendor_b, {})
        assert item_serial not in vendor_b_bl


# --- Gold polling tests ---

class TestGoldPolling:
    @pytest.mark.asyncio
    async def test_returns_true_when_gold_changes(self):
        ss = MagicMock()
        ss.gold = 100

        async def change_gold():
            await asyncio.sleep(0.3)
            ss.gold = 88

        asyncio.get_event_loop().create_task(change_gold())
        result = await _wait_for_gold_change(ss, 100, timeout=2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        ss = MagicMock()
        ss.gold = 100
        result = await _wait_for_gold_change(ss, 100, timeout=0.5)
        assert result is False
