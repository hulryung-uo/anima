"""Regression: drag_drop must not mistake a partial-stack lift for a rejection.

When the agent lifts part of a stack (e.g. 50 ingots out of a stack of 100,
or splits a gold pile), ServUO leaves the source serial in place at the same
container/position but with its ``amount`` decremented, and the lifted portion
goes onto the cursor. The lift-rejection guard snapshots the source before the
pick-up and compares it after; if it only compared position it would read the
shrunk stack as "did not move" and falsely return success=False, stranding the
move. The snapshot must include ``amount``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.actions.inventory import drag_drop
from anima.perception.world_state import ItemInfo, WorldState


def _ctx_with_item(item: ItemInfo) -> MagicMock:
    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    world = WorldState()
    world.items[item.serial] = item
    ctx.perception.world = world
    return ctx


@pytest.mark.asyncio
async def test_partial_stack_lift_is_success():
    """Lifting 50 from a stack of 100 (source amount drops to 50) succeeds."""
    item = ItemInfo(serial=0x4001, container=0x101, x=2, y=3, z=0, amount=100)
    ctx = _ctx_with_item(item)

    # Simulate the server's 0x25/0x3C echo: source stays put, amount shrinks.
    async def _send(_pkt: bytes) -> None:
        ctx.perception.world.items[0x4001].amount = 50

    ctx.conn.send_packet = AsyncMock(side_effect=_send)

    result = await drag_drop(ctx, item_serial=0x4001, amount=50, target_serial=0x102)

    assert result.success, result.message
    # pick_up + drop_item both went out (the lift was NOT short-circuited).
    assert ctx.conn.send_packet.call_count == 2


@pytest.mark.asyncio
async def test_unchanged_source_is_rejection():
    """A genuine 0x27 LiftRej leaves the source identical -> reported failure."""
    item = ItemInfo(serial=0x4002, container=0x101, x=2, y=3, z=0, amount=100)
    ctx = _ctx_with_item(item)
    # send_packet does nothing: the world entry never changes (rejected lift).

    result = await drag_drop(ctx, item_serial=0x4002, amount=50, target_serial=0x102)

    assert not result.success
    assert "rejected" in result.message.lower()
    # Only the pick_up was sent; the drop is skipped on a rejected lift.
    assert ctx.conn.send_packet.call_count == 1


@pytest.mark.asyncio
async def test_full_stack_lift_removed_is_success():
    """A full lift removes the source serial entirely -> success."""
    item = ItemInfo(serial=0x4003, container=0x101, x=2, y=3, z=0, amount=10)
    ctx = _ctx_with_item(item)

    async def _send(_pkt: bytes) -> None:
        ctx.perception.world.items.pop(0x4003, None)

    ctx.conn.send_packet = AsyncMock(side_effect=_send)

    result = await drag_drop(ctx, item_serial=0x4003, amount=10, target_serial=0x102)

    assert result.success
    assert ctx.conn.send_packet.call_count == 2
