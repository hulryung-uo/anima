"""Regression: wander_action must not eagerly pin SelfState.direction on a turn.

When wander_action's chosen direction differs from the current facing it queues
a turn-only walk packet, routing the new facing through ``_pending_direction``
so the walker applies it on the turn's own ConfirmWalk (0x22) and rolls it back
on a DenyWalk (0x21). The old code ALSO wrote ``self_state.direction`` directly
right after sending the turn — before the server had confirmed it.

That eager write races the server: if the turn is DENIED (Frozen / paralyzed /
blocked facing), ``WalkerManager.deny_walk`` resyncs ``SelfState.direction`` to
the server-authoritative facing it carries (and clears ``_pending_direction``),
but the eager write — which executes AFTER the synchronous deny — overwrites
that correction and leaves the avatar believing it faces a direction the server
just refused. Every later step that reads ``self_state.direction`` then starts
from a desynced facing.

``go_to`` / ``_walk_one_step`` never mutate ``self_state.direction`` in their
turn branch for exactly this reason; ``wander_action`` must match. This drives
the real ``wander_action`` with a fake server that DENIES the turn synchronously
(the moment the desync window opens) and asserts the facing reflects server
truth, not the optimistic local write.
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

NORTH = 0
EAST = 2


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
        # East = (sx+1, sy). Everything else blocked -> single candidate.
        return _AllowEastTile(x == self._sx + 1 and y == self._sy)


class _DenyingConn:
    """DENIES the FIRST walk packet (the turn) synchronously, like the server.

    The deny lands inside wander_action's ``send_packet`` await — before the old
    eager ``self_state.direction = direction`` line ran — so a regression leaves
    the facing pinned to the refused EAST instead of the server's NORTH.
    """

    def __init__(self, walker: WalkerManager, ss: SelfState) -> None:
        self.connected = True
        self._walker = walker
        self._ss = ss
        self._packets = 0

    async def send_packet(self, pkt: bytes) -> None:
        self._packets += 1
        if self._packets == 1:
            # pkt = [0x02][dir][seq][fastwalk:u32]; server refuses the turn and
            # reports the avatar still facing NORTH at its current tile.
            seq = pkt[2]
            self._walker.deny_walk(
                seq, x=self._ss.x, y=self._ss.y, z=self._ss.z, direction=NORTH,
            )


def _run(coro):
    return asyncio.run(coro)


def test_wander_turn_does_not_eagerly_pin_direction():
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = NORTH  # chosen wander dir is EAST -> forces a turn

    walker = WalkerManager(ss, EventStream())

    conn = _DenyingConn(walker, ss)
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

    _run(wander_action(ctx))

    # The server REFUSED the turn and resynced facing to NORTH. wander_action
    # must not have overwritten that with the optimistic EAST it requested.
    assert (ss.direction & 0x07) == NORTH
    # And the pending facing must have been dropped by the deny, not left to
    # snap a later, unrelated move's confirm to the refused direction.
    assert walker._pending_direction is None
