"""Regression: a denied turn must not leak its direction onto a later move.

Turn packets stamp ``_pending_direction`` and rely on the matching
``ConfirmWalk`` to apply it to ``SelfState.direction``.  If the server
*denies* the turn instead, ``deny_walk()`` has to drop the pending
direction; otherwise the rejected facing survives and the next confirmed
move (which never set ``_pending_direction``) wrongly applies it, snapping
the avatar to a direction the server already refused and desyncing
``SelfState.direction`` from the authoritative server position.
"""

from __future__ import annotations

from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import WalkerManager

NORTH = 0
EAST = 2


def _make_walker() -> WalkerManager:
    return WalkerManager(SelfState(serial=1), EventStream())


def test_deny_walk_clears_pending_direction():
    """deny_walk must drop the in-flight turn's pending direction."""
    w = _make_walker()

    # Send a turn toward NORTH: turn packets set _pending_direction and leave
    # _pending_step_tile = None, then hand out a sequence.
    w._pending_step_tile = None
    w._pending_direction = NORTH
    seq = w.next_sequence()

    # Server denies the turn (resyncs us facing EAST at the current tile).
    w.deny_walk(seq, x=100, y=100, z=0, direction=EAST)

    assert w._pending_direction is None
    # Deny is authoritative for direction.
    assert w._self_state.direction == EAST


def test_denied_turn_does_not_taint_next_confirmed_move():
    """The end-to-end bug: rejected turn dir must not ride the next move's confirm."""
    w = _make_walker()
    ss = w._self_state
    ss.x, ss.y, ss.direction = 100, 100, EAST

    # 1) Turn toward NORTH — pending direction set.
    w._pending_step_tile = None
    w._pending_direction = NORTH
    turn_seq = w.next_sequence()

    # 2) Server denies the turn; we stay facing EAST.
    w.deny_walk(turn_seq, x=100, y=100, z=0, direction=EAST)
    assert ss.direction == EAST

    # 3) Now perform a plain move EAST one tile. Move packets set
    #    _pending_step_tile but do NOT set _pending_direction.
    w._pending_step_tile = (101, 100)
    move_seq = w.next_sequence()

    # 4) Server confirms the move.
    w.confirm_walk(move_seq)

    # Position advanced as expected.
    assert (ss.x, ss.y) == (101, 100)
    # Direction must remain EAST — the denied NORTH turn must NOT have been
    # resurrected by the move's confirm.  Pre-fix, ss.direction == NORTH here.
    assert ss.direction == EAST
    assert w._pending_direction is None


def test_confirmed_turn_still_applies_direction():
    """Sanity: the normal (non-denied) turn path is unaffected by the fix."""
    w = _make_walker()
    ss = w._self_state
    ss.direction = EAST

    w._pending_step_tile = None
    w._pending_direction = NORTH
    seq = w.next_sequence()

    w.confirm_walk(seq)

    assert ss.direction == NORTH
    assert w._pending_direction is None
