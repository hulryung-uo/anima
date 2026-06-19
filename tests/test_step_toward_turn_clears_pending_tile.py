"""Regression: _step_toward's turn packet must not carry a stale move tile.

The brain's main per-tick movement loop (``_step_toward``) sends a turn-only
walk packet whenever the next step's direction differs from the current
facing, then sends the move in the same direction. A turn packet carries NO
position — but the loop used to stamp ``_pending_step_tile = (next_x, next_y)``
for *every* packet, turn included. When the server's ConfirmWalk (0x22) for
that turn arrives (seq matches the turn's stamped sequence),
``walker.confirm_walk`` then teleports the avatar onto the tile it merely
turned toward — a SelfState desync that poisons all subsequent pathfinding.

``go_to`` / ``wander_action`` / ``_walk_one_step`` already clear
``_pending_step_tile`` (and route the facing through ``_pending_direction``)
before a turn; ``_step_toward`` must too. This drives the real ``_step_toward``
with a fake server that acks the turn packet exactly when the desync fires.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.brain.behavior_tree import Status
from anima.brain.think import _step_toward
from anima.config import MovementConfig
from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import WalkerManager


class _OpenTile:
    def walkable_z(self, _current_z: int) -> tuple[bool, int]:
        return True, 0


class _FakeMap:
    """Everything is walkable, so find_path returns a straight eastward path."""

    def get_tile(self, _x: int, _y: int):
        return _OpenTile()

    def _get_item_flags(self, _graphic: int) -> int:  # pragma: no cover - no items
        return 0


class _FakeConn:
    """Acks the FIRST walk packet (the turn) synchronously, like the server.

    The turn confirm lands while _step_toward is still inside the move loop,
    the exact window the desync could fire if _pending_step_tile is stale.
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


def _make_ctx(ss: SelfState, walker: WalkerManager, conn: _FakeConn) -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(mobiles={}, items={}),
        ),
        walker=walker,
        map_reader=_FakeMap(),
        blackboard={},
        cfg=SimpleNamespace(movement=MovementConfig()),
        conn=conn,
    )


def test_step_toward_turn_does_not_apply_stale_pending_tile():
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = 0  # facing North; target is due East → first step forces a turn

    walker = WalkerManager(ss, EventStream())
    # Leftover from a prior un-acked move that was never confirmed/denied.
    walker._pending_step_tile = (200, 200)

    conn = _FakeConn(walker)
    ctx = _make_ctx(ss, walker, conn)

    status = _run(_step_toward(ctx, 105, 100))

    assert status is Status.SUCCESS
    # The turn's confirm fired mid-loop. The avatar must NOT have teleported to
    # the stale (200, 200) tile, nor jumped onto the as-yet-unconfirmed move
    # tile (101, 100). It stays at its real, server-authoritative position.
    assert (ss.x, ss.y) == (100, 100)
    # The turn's confirm should, however, have applied the new facing (East=2).
    assert ss.direction == 2


def test_step_toward_move_still_tracks_pending_tile():
    """When already facing the right way, the move packet keeps its tile.

    Guards against over-correcting the fix: a real move (is_turn False) must
    still stamp _pending_step_tile so confirm_walk advances position.
    """
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = 2  # already facing East → first step is a real move, no turn

    walker = WalkerManager(ss, EventStream())
    conn = _FakeConn(walker)
    ctx = _make_ctx(ss, walker, conn)

    _run(_step_toward(ctx, 105, 100))

    # The first (move) packet was acked; the avatar advanced one tile east.
    assert (ss.x, ss.y) == (101, 100)
