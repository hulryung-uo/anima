"""Regression: wander_action's turn packet must not carry a stale move tile.

A turn-only walk packet carries no position. ``wander_action`` queues a turn
when its chosen direction differs from the current facing, then sends the move
packet right after. The turn op gets its own sequence stamped onto
``_pending_seq``. If a previous, un-acked move left ``_pending_step_tile`` set,
that stale tile rides on the turn's sequence — and the turn's ConfirmWalk
(0x22), whose seq matches, then snaps the avatar onto a tile the server never
confirmed for this op. ``go_to`` / ``_walk_one_step`` already clear
``_pending_step_tile`` before a turn; ``wander_action`` must too.

This drives the real ``wander_action`` with a fake server that acks the turn
during the inter-step pause (exactly when the desync window opens).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.action.movement import wander_action
from anima.brain.behavior_tree import Status
from anima.config import MovementConfig
from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import WalkerManager


class _AllowEastTile:
    """Only the East-adjacent tile is walkable, so wander picks direction 2."""

    def __init__(self, walkable: bool) -> None:
        self._walkable = walkable

    def walkable_z(self, _current_z: int) -> tuple[bool, int]:
        return self._walkable, 0


class _FakeMap:
    def __init__(self, sx: int, sy: int) -> None:
        self._sx, self._sy = sx, sy

    def get_tile(self, x: int, y: int):
        # East = (sx+1, sy). Everything else blocked → single candidate.
        return _AllowEastTile(x == self._sx + 1 and y == self._sy)


class _FakeConn:
    """Acks the FIRST walk packet (the turn) synchronously, like the server.

    The turn confirm lands during wander_action's post-turn ``sleep(0.1)`` —
    the exact moment the desync could fire if _pending_step_tile is stale.
    """

    def __init__(self, walker: WalkerManager) -> None:
        self.connected = True
        self._walker = walker
        self._packets = 0

    async def send_packet(self, pkt: bytes) -> None:
        self._packets += 1
        if self._packets == 1:
            # pkt = [0x02][dir][seq][fastwalk:u32]
            seq = pkt[2]
            self._walker.confirm_walk(seq)


def _run(coro):
    return asyncio.run(coro)


def test_wander_turn_does_not_apply_stale_pending_tile():
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = 0  # facing North; chosen wander dir is East (2) → forces a turn

    walker = WalkerManager(ss, EventStream())
    # Leftover from a prior un-acked move that was never confirmed/denied.
    walker._pending_step_tile = (200, 200)

    conn = _FakeConn(walker)
    ctx = SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(mobiles={}, items={}),
        ),
        walker=walker,
        map_reader=_FakeMap(ss.x, ss.y),
        blackboard={},
        cfg=SimpleNamespace(movement=MovementConfig()),
        conn=conn,
    )

    status = _run(wander_action(ctx))

    assert status is Status.SUCCESS
    # The turn's confirm fired mid-step; the avatar must NOT have teleported to
    # the stale (200,200) tile. It stays at its real position (the move tile is
    # still unacked).
    assert (ss.x, ss.y) == (100, 100)
