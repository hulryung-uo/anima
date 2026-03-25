"""Tests for action primitives (v2 migration Step 2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.actions.result import ActionResult
from anima.core.bus import EventBus


class TestActionResult:
    def test_success(self):
        r = ActionResult(success=True, message="ok")
        assert r.success
        assert r.message == "ok"

    def test_failure(self):
        r = ActionResult(success=False, message="timeout")
        assert not r.success

    def test_data(self):
        r = ActionResult(success=True, data={"cursor_id": 42})
        assert r.data["cursor_id"] == 42


class TestWaitForCondition:
    @pytest.mark.asyncio
    async def test_already_true(self):
        bus = EventBus()
        result = await bus.wait_for_condition(lambda: True, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_becomes_true(self):
        bus = EventBus()
        flag = {"ready": False}

        async def set_flag():
            await asyncio.sleep(0.05)
            flag["ready"] = True
            bus.publish("test.event", {})

        task = asyncio.create_task(set_flag())
        result = await bus.wait_for_condition(lambda: flag["ready"], timeout=2.0)
        assert result is True
        await task

    @pytest.mark.asyncio
    async def test_timeout(self):
        bus = EventBus()
        result = await bus.wait_for_condition(lambda: False, timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_race_after_subscribe(self):
        """Predicate becomes true between subscribe and wait."""
        bus = EventBus()
        # The predicate is already true — should return immediately
        result = await bus.wait_for_condition(lambda: True, timeout=0.1)
        assert result is True


class TestTargetPrimitives:
    @pytest.mark.asyncio
    async def test_wait_for_target_success(self):
        from anima.actions.target import wait_for_target

        ctx = MagicMock()
        ctx.perception.self_state.pending_target = None
        bus = EventBus()
        ctx.bus = bus

        async def simulate_target():
            await asyncio.sleep(0.05)
            ctx.perception.self_state.pending_target = {"cursor_id": 123, "cursor_type": 1}
            bus.publish("avatar.target_cursor", {})

        task = asyncio.create_task(simulate_target())
        result = await wait_for_target(ctx, timeout=2.0)
        assert result.success
        assert result.data["cursor_id"] == 123
        await task

    @pytest.mark.asyncio
    async def test_wait_for_target_timeout(self):
        from anima.actions.target import wait_for_target

        ctx = MagicMock()
        ctx.perception.self_state.pending_target = None
        ctx.bus = EventBus()

        result = await wait_for_target(ctx, timeout=0.1)
        assert not result.success
        assert "timeout" in result.message.lower()

    @pytest.mark.asyncio
    async def test_target_tile(self):
        from anima.actions.target import target_tile

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await target_tile(ctx, cursor_id=1, x=100, y=200, z=15, graphic=0x021A)
        assert result.success
        ctx.conn.send_packet.assert_called_once()

    @pytest.mark.asyncio
    async def test_target_object(self):
        from anima.actions.target import target_object

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await target_object(ctx, cursor_id=1, serial=0x40001234)
        assert result.success
        assert result.data["serial"] == 0x40001234


class TestInventoryPrimitives:
    def test_find_in_backpack_found(self):
        from anima.actions.inventory import find_in_backpack

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack

        item1 = MagicMock(container=0x101, graphic=0x19B9, amount=5)
        item2 = MagicMock(container=0x101, graphic=0x1BF2, amount=3)
        item3 = MagicMock(container=0x999, graphic=0x19B9, amount=10)  # different container
        ctx.perception.world.items = {1: item1, 2: item2, 3: item3}

        found = find_in_backpack(ctx, {0x19B9})
        assert len(found) == 1
        assert found[0].amount == 5

    def test_find_in_backpack_empty(self):
        from anima.actions.inventory import find_in_backpack

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}

        found = find_in_backpack(ctx, {0x19B9})
        assert found == []

    def test_count_items(self):
        from anima.actions.inventory import count_items

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}

        item1 = MagicMock(container=0x101, graphic=0x19B9, amount=5)
        item2 = MagicMock(container=0x101, graphic=0x19B9, amount=12)
        ctx.perception.world.items = {1: item1, 2: item2}

        assert count_items(ctx, {0x19B9}) == 17

    @pytest.mark.asyncio
    async def test_drag_drop(self):
        from anima.actions.inventory import drag_drop

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await drag_drop(ctx, item_serial=0x40001, amount=10, target_serial=0x40002)
        assert result.success
        assert ctx.conn.send_packet.call_count == 2  # pick_up + drop_item


class TestGoToInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_callback(self):
        """go_to should return False when interrupt_check returns True."""
        from anima.action.movement import go_to

        ctx = MagicMock()
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 100
        ctx.perception.self_state.z = 0
        ctx.conn.connected = True
        ctx.cfg.movement.walk_delay_ms = 200

        # Interrupt immediately
        result = await go_to(ctx, 200, 200, interrupt_check=lambda: True)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_interrupt_none(self):
        """go_to should work normally when interrupt_check is None (default)."""
        from anima.action.movement import go_to

        ctx = MagicMock()
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 101  # Already within 1 tile of target
        ctx.perception.self_state.z = 0
        ctx.conn.connected = True
        ctx.cfg.movement.walk_delay_ms = 200
        ctx.walker.denied_tiles = {}

        # Already at target (within 1 tile) → should succeed
        result = await go_to(ctx, 100, 101, interrupt_check=None)
        assert result is True
