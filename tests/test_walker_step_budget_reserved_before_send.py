"""Regression: the outstanding-step budget must be reserved BEFORE the send.

Every move/turn send site sets ``_pending_step_tile`` / ``_pending_seq`` and
stamps the walk sequence, then calls ``conn.send_packet(...)``. That send
suspends on ``writer.drain()``; while it is suspended the recv_loop coroutine
runs and can dispatch THIS step's ConfirmWalk (0x22), calling
``walker.confirm_walk(seq)`` — which decrements ``steps_count``.

The send sites used to increment ``steps_count`` only AFTER the await. So a
confirm that arrived inside the drain window decremented the *pre-increment*
count (a no-op at 0) and the post-await ``+= 1`` then left ``steps_count`` one
too high. Over a fast/mounted run, where confirms routinely land inside the
drain window, ``steps_count`` drifts up to ``MAX_STEP_COUNT`` and
``can_walk()`` wedges movement until the 5 s stale auto-reset.

This drives the real ``_walk_one_step`` with a fake connection that acks the
move packet synchronously inside ``send_packet`` — exactly the "confirm during
the send await" race. After one fully-confirmed step the outstanding-step
budget must be back to zero.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.action.movement import _walk_one_step
from anima.config import MovementConfig
from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import WalkerManager


class _ConfirmDuringSendConn:
    """Confirms the walk packet synchronously inside send_packet().

    This models the recv_loop dispatching the step's ConfirmWalk while the
    sender is suspended in writer.drain(): the confirm is observed before the
    sender's post-send code (the buggy increment site) runs.
    """

    def __init__(self, walker: WalkerManager, self_state: SelfState) -> None:
        self.connected = True
        self._walker = walker
        self._ss = self_state

    async def send_packet(self, pkt: bytes) -> None:
        # Yield first so this faithfully runs as if on a separate loop turn,
        # the way drain() hands control to recv_loop.
        await asyncio.sleep(0)
        seq = pkt[2]  # [0x02][dir][seq][fastwalk:u32]
        self._walker.confirm_walk(seq)


def _make_ctx(ss: SelfState, walker: WalkerManager, conn) -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(mobiles={}, items={}),
        ),
        walker=walker,
        blackboard={},
        cfg=SimpleNamespace(movement=MovementConfig()),
        conn=conn,
    )


def test_step_budget_returns_to_zero_when_confirm_lands_during_send():
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = 2  # already facing East → first step is a pure move, no turn

    walker = WalkerManager(ss, EventStream())
    conn = _ConfirmDuringSendConn(walker, ss)
    ctx = _make_ctx(ss, walker, conn)

    asyncio.run(_walk_one_step(ctx, direction=2, step_delay=0.0))

    # The step was reserved before the send and the confirm released it during
    # the send, so the outstanding-step budget is balanced back to zero. Under
    # the old ordering it would be stranded at 1 (the confirm decremented the
    # pre-increment count, then the post-send `+= 1` re-inflated it).
    assert walker.steps_count == 0
    # And the confirmed move advanced the avatar one tile east.
    assert (ss.x, ss.y) == (101, 100)


def test_step_budget_does_not_drift_across_many_confirmed_steps():
    """Across a run of steps each acked mid-send, the budget never climbs.

    This is the cumulative form of the bug: each step would leak one unit of
    budget, so after MAX_STEP_COUNT steps can_walk() would refuse to walk.
    """
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = 2  # facing East throughout

    walker = WalkerManager(ss, EventStream())
    conn = _ConfirmDuringSendConn(walker, ss)
    ctx = _make_ctx(ss, walker, conn)

    async def _run_many() -> None:
        for _ in range(8):  # > MAX_STEP_COUNT (5)
            # Clear the per-step throttle so can_walk() never blocks the test.
            walker.last_step_time = 0.0
            await _walk_one_step(ctx, direction=2, step_delay=0.0)

    asyncio.run(_run_many())

    # No leaked budget — steps_count never drifted up past MAX_STEP_COUNT.
    # (Under the old ordering it climbed one per step and pinned can_walk()
    # off once it reached MAX_STEP_COUNT.)
    assert walker.steps_count == 0
    # With the per-step throttle cleared, the walker is free to walk again —
    # i.e. the budget, not a leaked count, is what gates it.
    walker.last_step_time = 0.0
    assert walker.can_walk()
