"""Regression tests: ConfirmWalk/DenyWalk must match the in-flight sequence.

The walk protocol allows up to MAX_STEP_COUNT pending steps. ``_pending_step_tile``
(and ``_pending_direction``) only describe the *most recently queued* op. If an
earlier ack — e.g. a turn-only ConfirmWalk that carries no position — arrives
after the next move packet has already overwritten ``_pending_step_tile``, the
old code applied the move tile on the turn's confirm and teleported the avatar
to a tile the server had not yet confirmed.  ConfirmWalk/DenyWalk now only apply
their effect when the acked ``seq`` matches the pending op's ``seq``.
"""

from __future__ import annotations

from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import WalkerManager


def _make_walker() -> WalkerManager:
    ss = SelfState(serial=1)
    ss.x, ss.y, ss.z, ss.direction = 100, 100, 0, 0
    return WalkerManager(ss, EventStream())


def test_confirm_applies_position_on_matching_seq():
    """The happy path: a confirm whose seq matches the pending move moves us."""
    w = _make_walker()
    w._pending_step_tile = (101, 100)
    move_seq = w.next_sequence()
    w.steps_count += 1

    w.confirm_walk(move_seq)

    assert (w._self_state.x, w._self_state.y) == (101, 100)
    assert w._pending_step_tile is None
    assert w.steps_count == 0


def test_stale_turn_confirm_does_not_apply_next_move_tile():
    """A turn confirm arriving after the move was queued must NOT move us.

    Reproduces the desync: send a turn (seq T, no tile), then before its
    confirm arrives, queue a move (seq M, tile) which overwrites
    _pending_step_tile. The turn's ConfirmWalk(T) must only decrement the
    step budget, never apply the move's tile.
    """
    w = _make_walker()

    # 1) Turn packet — no position, _pending_step_tile stays None.
    w._pending_step_tile = None
    w._pending_direction = 2
    turn_seq = w.next_sequence()
    w.steps_count += 1

    # 2) Move packet queued before the turn confirm arrives — this overwrites
    #    _pending_step_tile / _pending_seq with the move's values.
    w._pending_step_tile = (101, 100)
    move_seq = w.next_sequence()
    w.steps_count += 1
    assert turn_seq != move_seq

    # 3) The turn's confirm finally arrives (stale relative to the pending move).
    w.confirm_walk(turn_seq)

    # Avatar must NOT have jumped to the move tile — the move is still unacked.
    assert (w._self_state.x, w._self_state.y) == (100, 100)
    # The move's pending state is preserved for its own confirm.
    assert w._pending_step_tile == (101, 100)
    assert w._pending_seq == move_seq
    # The outstanding-step budget still drops by one (one packet was acked).
    assert w.steps_count == 1

    # 4) Now the move's own confirm arrives → position finally updates.
    w.confirm_walk(move_seq)
    assert (w._self_state.x, w._self_state.y) == (101, 100)
    assert w._pending_step_tile is None
    assert w.steps_count == 0


def test_stale_deny_does_not_blame_a_later_pending_tile():
    """A deny for an old seq must resync but not record the later tile as denied."""
    w = _make_walker()

    # Old in-flight op (seq O) with its own tile.
    w._pending_step_tile = (101, 100)
    old_seq = w.next_sequence()
    w.steps_count += 1

    # A newer op (seq N) overwrites _pending_step_tile before the old deny lands.
    w._pending_step_tile = (100, 99)
    new_seq = w.next_sequence()
    w.steps_count += 1
    assert old_seq != new_seq

    # Server denies the OLD seq, reporting authoritative coords.
    w.deny_walk(old_seq, 100, 100, 0, 0)

    # The later tile (100, 99) must NOT be in the denied cache — it was never
    # the tile the server rejected.
    assert not w.is_tile_denied(100, 99)
    # Position resynced to the server's coordinates.
    assert (w._self_state.x, w._self_state.y) == (100, 100)
    # Server zeroes its sequence on any deny; ours must follow.
    assert w.walk_sequence == 0


def test_matching_deny_records_the_denied_tile():
    """A deny whose seq matches the pending move records that tile as denied."""
    w = _make_walker()
    w._pending_step_tile = (101, 100)
    move_seq = w.next_sequence()
    w.steps_count += 1

    w.deny_walk(move_seq, 100, 100, 0, 0)

    assert w.is_tile_denied(101, 100)
    assert w._pending_step_tile is None
    assert w.walk_sequence == 0
