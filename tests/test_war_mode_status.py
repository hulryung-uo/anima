"""Tests for 0x72 WarMode — war state must land in flags, not corrupt direction."""

from anima.client.handler import PacketHandler
from anima.client.packets import build_war_mode
from anima.perception import Perception
from anima.perception.enums import MobileFlags
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def test_war_mode_on_sets_flag_and_preserves_direction():
    h, p, w = _make_stack()
    # Player is facing SOUTH (direction 4); walker/movement read this as 0-7.
    p.self_state.direction = 4

    h.dispatch(0x72, build_war_mode(True))

    # War mode is recorded in flags and queryable.
    assert p.self_state.in_war_mode is True
    assert bool(p.self_state.flags & MobileFlags.WAR_MODE) is True
    # Direction must remain an untouched 0-7 facing (the old code OR-ed 0x80
    # into it, turning 4 into 132 and breaking facing-dependent walk/combat).
    assert p.self_state.direction == 4


def test_war_mode_off_clears_flag():
    h, p, w = _make_stack()
    p.self_state.flags |= MobileFlags.WAR_MODE

    h.dispatch(0x72, build_war_mode(False))

    assert p.self_state.in_war_mode is False
    assert bool(p.self_state.flags & MobileFlags.WAR_MODE) is False


def test_war_mode_toggle_does_not_disturb_other_flags():
    h, p, w = _make_stack()
    # Hidden bit set independently (e.g. from a self 0x78).
    p.self_state.flags |= MobileFlags.HIDDEN

    h.dispatch(0x72, build_war_mode(True))
    assert p.self_state.in_war_mode is True
    assert p.self_state.hidden is True  # hidden untouched

    h.dispatch(0x72, build_war_mode(False))
    assert p.self_state.in_war_mode is False
    assert p.self_state.hidden is True  # still untouched


def test_war_mode_does_not_set_running_bit_on_direction():
    h, p, w = _make_stack()
    p.self_state.direction = 2  # EAST

    h.dispatch(0x72, build_war_mode(True))

    # The RUNNING bit (0x80) must never appear in direction as a side effect.
    assert p.self_state.direction & 0x80 == 0
    assert (p.self_state.direction & 0x07) == 2
