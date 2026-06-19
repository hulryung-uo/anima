"""Regression: _step_toward must not eagerly pin SelfState.direction on a turn.

The brain's main per-tick movement loop (``_step_toward``) sends a turn-only
walk packet whenever the next step's direction differs from the current facing,
routing the new facing through ``_pending_direction`` so the walker applies it
on the turn's own ConfirmWalk (0x22) and rolls it back on a DenyWalk (0x21).
The old code ALSO wrote ``self_state.direction`` directly right after sending
the turn — before the server had confirmed it.

That eager write races the server: if the turn is DENIED (Frozen / paralyzed /
blocked facing), ``WalkerManager.deny_walk`` resyncs ``SelfState.direction`` to
the server-authoritative facing it carries (and clears ``_pending_direction``),
but the eager write — which executes AFTER the synchronous deny — overwrites
that correction and leaves the avatar believing it faces a direction the server
just refused. Every later chase step that reads ``self_state.direction`` then
starts from a desynced facing.

``go_to`` / ``wander_action`` / ``_walk_one_step`` never mutate
``self_state.direction`` in their turn branch for exactly this reason;
``_step_toward`` must match. This drives the real ``_step_toward`` with a fake
server that DENIES the turn synchronously (the moment the desync window opens)
and asserts the facing reflects server truth, not the optimistic local write.
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

NORTH = 0
EAST = 2


class _OpenTile:
    def walkable_z(self, _current_z: int) -> tuple[bool, int]:
        return True, 0


class _FakeMap:
    """Everything walkable, so find_path returns a straight eastward path."""

    def get_tile(self, _x: int, _y: int):
        return _OpenTile()

    def _get_item_flags(self, _graphic: int) -> int:  # pragma: no cover - no items
        return 0


class _DenyingConn:
    """DENIES the FIRST walk packet (the turn) synchronously, like the server.

    The deny lands inside _step_toward's ``send_packet`` await — before the old
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


def _make_ctx(ss: SelfState, walker: WalkerManager, conn) -> SimpleNamespace:
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


def test_step_toward_turn_does_not_eagerly_pin_direction():
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = NORTH  # target is due East -> first step forces a turn

    walker = WalkerManager(ss, EventStream())
    conn = _DenyingConn(walker, ss)
    ctx = _make_ctx(ss, walker, conn)

    _run(_step_toward(ctx, 105, 100))

    # The server REFUSED the turn and resynced facing to NORTH. _step_toward
    # must not have overwritten that with the optimistic EAST it requested.
    assert (ss.direction & 0x07) == NORTH
    # And the pending facing must have been dropped by the deny, not left to
    # snap a later, unrelated move's confirm to the refused direction.
    assert walker._pending_direction is None


def test_step_toward_turn_still_applies_facing_on_confirm():
    """Guard against over-correcting: a CONFIRMED turn must still apply facing.

    When the server ACKs the turn, the facing routed through _pending_direction
    is applied by confirm_walk — removing the eager write must not lose that.
    """
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.direction = NORTH  # target due East -> turn first

    walker = WalkerManager(ss, EventStream())

    class _ConfirmTurnConn:
        def __init__(self) -> None:
            self.connected = True
            self._packets = 0

        async def send_packet(self, pkt: bytes) -> None:
            self._packets += 1
            if self._packets == 1:  # ack the turn only
                walker.confirm_walk(pkt[2])

    ctx = _make_ctx(ss, walker, _ConfirmTurnConn())
    _run(_step_toward(ctx, 105, 100))

    # The turn's confirm applied the new facing (East), and position did not
    # jump (the move packet is unconfirmed in this fake).
    assert (ss.direction & 0x07) == EAST
    assert (ss.x, ss.y) == (100, 100)
