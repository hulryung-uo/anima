"""Regression tests for WalkerManager stale-walk auto-reset.

If the server silently stops responding to walk packets (no ConfirmWalk
0x22 and no DenyWalk 0x21), ``steps_count`` climbs to ``MAX_STEP_COUNT``
and ``can_walk()`` must eventually recover by auto-resetting, otherwise
the agent is wedged forever.  That recovery is gated on ``_last_step_sent``
being non-zero, which only happens if every sent walk packet stamps it.
"""

from __future__ import annotations

from anima.perception.event_stream import EventStream
from anima.perception.self_state import SelfState
from anima.perception.walker import MAX_STEP_COUNT, WalkerManager


def _make_walker() -> WalkerManager:
    return WalkerManager(SelfState(serial=1), EventStream())


def test_next_sequence_stamps_last_step_sent():
    """Handing out a sequence (always done right before a send) arms the clock."""
    w = _make_walker()
    assert w._last_step_sent == 0.0
    w.next_sequence()
    assert w._last_step_sent > 0.0


def test_can_walk_auto_resets_when_server_goes_silent():
    """A full step pipeline with no confirm/deny must self-heal after 5s."""
    w = _make_walker()

    # Simulate sending MAX_STEP_COUNT walk packets with no confirm/deny.
    for _ in range(MAX_STEP_COUNT):
        w.next_sequence()
        w.steps_count += 1

    # Pending steps are maxed out and no throttle blocks us — but the
    # step budget is exhausted, so we cannot walk.
    w.last_step_time = 0.0
    assert w.steps_count == MAX_STEP_COUNT
    assert w.can_walk() is False

    # Pretend the last packet was sent >5s ago and the server never replied.
    w._last_step_sent -= 6000
    w.last_step_time = 0.0

    # can_walk() must detect the stale state, reset the step budget, and
    # let movement resume — instead of wedging forever.
    assert w.can_walk() is True
    assert w.steps_count == 0
    assert w.walk_sequence == 0
    assert w._last_step_sent == 0.0


def test_can_walk_does_not_reset_when_recently_active():
    """A recent send must NOT trigger the stale-state reset."""
    w = _make_walker()
    for _ in range(MAX_STEP_COUNT):
        w.next_sequence()
        w.steps_count += 1
    w.last_step_time = 0.0

    # Last packet was sent just now — not stale.
    assert w.can_walk() is False
    assert w.steps_count == MAX_STEP_COUNT


def test_can_walk_never_resets_with_unarmed_clock():
    """Guard against the old bug: clock at 0.0 must never falsely look stale."""
    w = _make_walker()
    # Manually wedge steps_count without ever stamping the clock.
    w.steps_count = MAX_STEP_COUNT
    w._last_step_sent = 0.0
    w.last_step_time = 0.0
    # With an unarmed clock there is nothing to reset from, so we stay blocked
    # (this is the pre-fix state for a path that never called next_sequence).
    assert w.can_walk() is False
    assert w.steps_count == MAX_STEP_COUNT


def test_stale_reset_clears_pending_so_late_ack_cannot_teleport():
    """A delayed ack arriving after the stale auto-reset must NOT move us.

    Repro of the desync: a move (seq M, tile T) is queued, the server goes
    quiet long enough to trip the 5s auto-reset, then the server's merely-slow
    ConfirmWalk(M) finally lands. If the reset left _pending_seq / _pending_*
    dangling, that late confirm matches the lingering seq and snaps the avatar
    onto T — a tile from the step we already abandoned. After the fix the
    pending op state is cleared, so the late ack is treated as a non-pending
    stale ack and leaves the position untouched.
    """
    w = _make_walker()
    w._self_state.x, w._self_state.y = 100, 100

    # Queue a single move whose confirm never arrives.
    w._pending_step_tile = (101, 100)
    move_seq = w.next_sequence()
    w.steps_count += 1

    # Server goes silent past the 5s threshold → auto-reset on the next poll.
    w._last_step_sent -= 6000
    w.last_step_time = 0.0
    assert w.can_walk() is True
    assert w.steps_count == 0
    assert w.walk_sequence == 0
    # The pending op must be fully forgotten by the reset.
    assert w._pending_seq is None
    assert w._pending_step_tile is None
    assert w._pending_direction is None

    # The slow server finally confirms the abandoned step.
    w.confirm_walk(move_seq)

    # Avatar must stay where it is — the late ack is stale, not pending.
    assert (w._self_state.x, w._self_state.y) == (100, 100)


def test_stale_reset_clears_pending_direction_so_late_turn_ack_cannot_stick():
    """A delayed turn confirm after the auto-reset must not apply a stale facing."""
    w = _make_walker()
    w._self_state.direction = 0

    # Queue a turn (no tile) whose confirm never arrives.
    w._pending_step_tile = None
    w._pending_direction = 4  # South
    turn_seq = w.next_sequence()
    w.steps_count += 1

    # Trip the stale auto-reset.
    w._last_step_sent -= 6000
    w.last_step_time = 0.0
    assert w.can_walk() is True
    assert w._pending_direction is None

    # The slow turn confirm lands after the reset.
    w.confirm_walk(turn_seq)

    # Facing must remain the server-authoritative pre-turn direction.
    assert w._self_state.direction == 0
