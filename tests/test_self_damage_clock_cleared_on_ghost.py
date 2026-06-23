"""The recent-damage clock must reset on the living→ghost (death) transition.

``last_damage_taken_at`` is the "we were recently attacked" stamp the
defensive-disposition melee gate reads (AttackSkill.can_execute fights back only
when ``now - last_damage_taken_at <= DEFENSIVE_WINDOW``). Death carries no event
that resets it, so an agent that dies mid-fight and is resurrected inside the
window would revive at full HP and immediately re-engage the swarm that killed
it. SelfState.set_body() clears the stamp on the death transition, alongside the
poison / open-container / vendor / pending-target clears. We drive set_body
directly (the unit under test) so the test pins the state machine, not a packet
codec path.
"""
import time

from anima.perception.self_state import SelfState
from anima.perception.world_state import _GHOST_BODIES

LIVING_BODY = 0x0190
GHOST_BODY = next(iter(_GHOST_BODIES))


def _recently_hit_self() -> SelfState:
    ss = SelfState(serial=0x1)
    ss.body = LIVING_BODY
    # Just took a hit a moment ago — well inside any defensive window.
    ss.last_damage_taken_at = time.monotonic()
    return ss


def test_ghost_body_resets_damage_clock():
    ss = _recently_hit_self()
    ss.set_body(GHOST_BODY)
    assert ss.is_ghost is True
    assert ss.last_damage_taken_at == 0.0


def test_damage_clock_stays_clear_across_resurrect():
    ss = _recently_hit_self()
    ss.set_body(GHOST_BODY)
    assert ss.last_damage_taken_at == 0.0
    # Resurrect: body flips back to living (no new swing) — must stay clear so a
    # full-HP revived agent does not read a pre-death hit as "recently attacked".
    ss.set_body(LIVING_BODY)
    assert ss.is_ghost is False
    assert ss.last_damage_taken_at == 0.0


def test_living_to_living_body_change_keeps_damage_clock():
    """A non-death body change (polymorph/mount) must NOT wipe the clock."""
    ss = _recently_hit_self()
    stamp = ss.last_damage_taken_at
    ss.set_body(0x0191)  # another living body
    assert ss.is_ghost is False
    assert ss.last_damage_taken_at == stamp


def test_ghost_to_ghost_does_not_re_clear():
    """Only the living→ghost edge resets; a ghost→ghost update is a no-op edge."""
    ss = _recently_hit_self()
    ss.set_body(GHOST_BODY)
    # A later hit-stamp landing while still a ghost (out-of-order packet) must not
    # be wiped by a second ghost-body update that is not a fresh death edge.
    ss.last_damage_taken_at = 123.0
    ss.set_body(GHOST_BODY)
    assert ss.last_damage_taken_at == 123.0
