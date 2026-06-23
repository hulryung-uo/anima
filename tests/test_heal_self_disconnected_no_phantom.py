"""A heal-potion quaff sent on a dead connection is NOT a success.

heal_self is the planner's last-resort survival heal (Priority-1 critical-HP
ladder). A disconnected ``send_packet`` is a silent no-op, so a quaff issued
while the session is down drank nothing. The old execute() still:
  * stamped ``_potion_last_ts`` — parking the can_start use-delay for the full
    POTION_COOLDOWN_S so the agent would NOT retry the heal for ~10s, and
  * returned success=True — a phantom survival win.

Both are fatal in the critical-HP ladder: the planner is told the heal landed
and the cooldown blocks the only retry while the agent bleeds out. This pins:
  * disconnected   -> success=False (retryable), no cooldown stamped, no send
  * connected      -> success=True, cooldown stamped, packet sent (regression
                      guard that the new early-return didn't break the happy path)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.perception import Perception
from anima.perception.world_state import ItemInfo
from anima.procedures.base import FailureReason
from anima.procedures.combat_loop import HEAL_POTION_GRAPHICS
from anima.procedures.heal_self import HealSelf

_BACKPACK_LAYER = 0x15
_BACKPACK_SERIAL = 0xDEAD0001
_HEAL_POTION_GRAPHIC = next(iter(HEAL_POTION_GRAPHICS))


def _build_ctx(*, connected: bool):
    perception = Perception(player_serial=0x1000)
    ss = perception.self_state
    ss.serial = 0x1000
    ss.hits = 30
    ss.hits_max = 100
    ss.is_poisoned = False
    ss.equipment[_BACKPACK_LAYER] = _BACKPACK_SERIAL
    perception.world.items[0xABCD] = ItemInfo(
        serial=0xABCD,
        graphic=_HEAL_POTION_GRAPHIC,
        container=_BACKPACK_SERIAL,
        amount=1,
    )
    conn = SimpleNamespace(connected=connected, send_packet=AsyncMock())
    bus = None  # execute() guards `if ctx.bus is not None` before using it
    return SimpleNamespace(
        perception=perception, blackboard={}, conn=conn, bus=bus,
    )


@pytest.mark.asyncio
async def test_disconnected_quaff_is_retryable_failure_no_cooldown():
    proc = HealSelf()
    ctx = _build_ctx(connected=False)

    result = await proc.execute(ctx)

    assert result.success is False, (
        "a quaff sent on a dead connection drank nothing — must not be a success"
    )
    assert result.reason == FailureReason.INTERRUPTED
    # No packet was sent into the dead socket...
    ctx.conn.send_packet.assert_not_awaited()
    # ...and crucially the cooldown was NOT stamped, so the agent can retry the
    # heal the instant the session reconnects instead of waiting POTION_COOLDOWN_S.
    assert "_potion_last_ts" not in ctx.blackboard


@pytest.mark.asyncio
async def test_connected_quaff_still_succeeds_and_stamps_cooldown():
    """Happy-path regression: the new early-return must not break a live quaff."""
    proc = HealSelf()
    ctx = _build_ctx(connected=True)

    result = await proc.execute(ctx)

    assert result.success is True
    ctx.conn.send_packet.assert_awaited_once()
    assert "_potion_last_ts" in ctx.blackboard
