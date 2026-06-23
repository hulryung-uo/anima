"""Resource-leak guard: MineOre must release its bus subscriptions on EVERY
exit path, including a cancellation mid-swing.

MineOre.execute subscribes two callbacks (``avatar.speech_heard`` and
``avatar.speech_cliloc``) to read the per-swing result clilocs, then waits in
a poll loop that suspends on ``asyncio.sleep`` — a cancellation point. The
planner runs the procedure as a bare ``create_task`` and cancels it routinely
(liveness watchdog, override/survival preemption). Before the fix the
unsubscribe ran as a plain statement AFTER the wait loop, so a cancel mid-sleep
skipped it and leaked both subscriptions permanently. mine_ore is the dominant
profession loop, so ``EventBus._subs`` grew without bound across a session —
and since ``publish()`` (driven by every packet handler) iterates ``_subs``,
the leak also degraded dispatch O(n).

This test drives many cancelled swings against a real EventBus and asserts the
subscriber count stays bounded (returns to baseline) instead of growing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.core.bus import EventBus
from anima.perception.social_state import SocialState
from anima.procedures.mine_ore import MineOre


def _make_ctx(bus: EventBus):
    ctx = MagicMock()
    ctx.perception.self_state.x = 2500
    ctx.perception.self_state.y = 550
    ctx.perception.self_state.z = 15
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: pickaxe}
    ctx.perception.social = SocialState()
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = bus
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_cancel_mid_swing_does_not_leak_bus_subscriptions():
    bus = EventBus()
    baseline = bus.subscriber_count

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    async def fake_sleep(_d):
        # The first suspension inside the swing wait loop is where the planner's
        # task.cancel() lands — model it as a CancelledError at the await point.
        raise asyncio.CancelledError()

    # Run a batch of cancelled swings. If the unsubscribe is not in a finally,
    # each one leaks two subscriptions and subscriber_count climbs by 2/swing.
    for _ in range(25):
        proc = MineOre()
        ctx = _make_ctx(bus)
        with patch(
            "anima.procedures.mine_ore._find_mineable_tile",
            return_value=(2500, 550, 15, 220, False),
        ), patch(
            "anima.procedures.mine_ore.use_on_target", new=_use_on_target,
        ), patch(
            "anima.procedures.mine_ore.asyncio.sleep", new=fake_sleep,
        ):
            with pytest.raises(asyncio.CancelledError):
                await proc.execute(ctx)

    # Every cancelled swing must have torn down both of its subscriptions.
    assert bus.subscriber_count == baseline, (
        f"bus leaked {bus.subscriber_count - baseline} subscriptions across 25 "
        "cancelled mining swings (unsubscribe must run in a finally)"
    )
