"""A target cursor outstanding at death must clear on the ghost transition.

pending_target (set by the 0x6C target-cursor handler) is only cleared by a
matching cancel, which ServUO does not echo for our own char around death. A
stale cursor then survives into the ghost/resurrect period, so the next skill/
spell polling it fires at a serial captured before death. SelfState.set_body()
clears it on the living→ghost transition, alongside the poison / open-container
/ vendor clears. We drive set_body directly (the unit under test) rather than a
raw 0x6C packet so the test pins the state-machine, not the packet codec.
"""
from anima.perception.self_state import SelfState
from anima.perception.world_state import _GHOST_BODIES

LIVING_BODY = 0x0190
GHOST_BODY = next(iter(_GHOST_BODIES))


def _poisoned_targeting_self() -> SelfState:
    ss = SelfState(serial=0x1)
    ss.body = LIVING_BODY
    ss.pending_target = {"cursor_id": 0x55, "serial": 0xDEAD}
    return ss


def test_ghost_body_clears_pending_target():
    ss = _poisoned_targeting_self()
    ss.set_body(GHOST_BODY)
    assert ss.is_ghost is True
    assert ss.pending_target is None


def test_pending_target_stays_clear_across_resurrect():
    ss = _poisoned_targeting_self()
    ss.set_body(GHOST_BODY)
    assert ss.pending_target is None
    # Resurrect: body flips back to living (no new 0x6C) — must stay clear.
    ss.set_body(LIVING_BODY)
    assert ss.is_ghost is False
    assert ss.pending_target is None


def test_living_to_living_body_change_keeps_pending_target():
    """A non-death body change (polymorph/mount) must NOT wipe a live cursor."""
    ss = _poisoned_targeting_self()
    ss.set_body(0x0191)  # another living body
    assert ss.is_ghost is False
    assert ss.pending_target == {"cursor_id": 0x55, "serial": 0xDEAD}
