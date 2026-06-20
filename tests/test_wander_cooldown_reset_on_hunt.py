"""Regression: the WanderForCombat post-yield cooldown must not outlive an
intervening real fight.

When a wander sweep declares the arena swept-clean it parks
``_wander_cooldown_until`` ~30s in the future so wander stays off and the
productive tail runs (suppressing the wander<->work flap). HuntNearby is NOT
gated by that cooldown, so a mob can wander into range and the agent fights it.
A genuine fight proves the spot is NOT swept clean, so the stale yield cooldown
must be cleared at engagement start — otherwise WanderForCombat.can_start keeps
refusing to roam for the rest of the stale window and the warrior falls through
to the mining chain instead of re-engaging the active arena.

HuntNearby clears it via ``_reset_engagement_state`` (mirrors the existing
``_wander_empty`` reset and the wander-spotted-a-target clear in execute()).
"""
from types import SimpleNamespace

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import (
    WanderForCombat,
    _reset_engagement_state,
)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    # Pin both clocks the procedure reads so the cooldown comparison is
    # deterministic (can_start reads time.time(); execute reads time.monotonic
    # via the autouse harness convention). Neither real sleep is reached here.
    async def _instant(_s):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant)
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(cl.time, "time", lambda: 1_000.0)


def _ctx():
    ss = SimpleNamespace(
        is_alive=True, hits_max=100, hp_percent=100.0,
        x=100, y=100, serial=0x1, equipment={1: 0x999},
    )
    world = SimpleNamespace(nearby_mobiles=lambda x, y, distance=0: [])
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
    )


def test_reset_engagement_state_clears_wander_cooldown():
    ctx = _ctx()
    # A prior wander yield parked the cooldown well into the future.
    ctx.blackboard["_wander_cooldown_until"] = 1_000.0 + 30.0
    _reset_engagement_state(ctx)
    assert ctx.blackboard["_wander_cooldown_until"] == 0.0


@pytest.mark.asyncio
async def test_cooldown_does_not_suppress_wander_after_a_hunt(monkeypatch):
    """A wander yield parks the cooldown; a fight happens; wander must be free to
    resume immediately afterward (the fight proved the spot is still active)."""
    # No hostile in range anywhere — wander would roam, hunt would not start.
    monkeypatch.setattr(cl, "_find_target", lambda ctx: None)

    ctx = _ctx()
    proc = WanderForCombat()

    # 1) A prior wander sweep yielded and parked the post-yield cooldown.
    ctx.blackboard["_wander_cooldown_until"] = 1_000.0 + 30.0
    # While the cooldown is live, wander correctly stays off (flap suppression).
    assert await proc.can_start(ctx) is False

    # 2) A mob wandered in and a real fight ran (engagement start clears scratch).
    _reset_engagement_state(ctx)
    assert ctx.blackboard["_wander_cooldown_until"] == 0.0

    # 3) The fight ended with no target in immediate range but the arena is still
    #    active. Wander MUST be allowed to resume — before the fix the stale
    #    cooldown kept can_start False for the rest of the window, stranding the
    #    warrior in the mining fallthrough.
    assert await proc.can_start(ctx) is True
