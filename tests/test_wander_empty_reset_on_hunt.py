"""Regression: the WanderForCombat consecutive-empty-roam counter must not leak
across an intervening real fight.

``_wander_empty`` counts CONSECUTIVE empty wander sweeps; once it reaches
WANDER_MAX_EMPTY the arena is deemed swept-clean and wander yields to productive
work. A genuine fight (HuntNearby) proves the spot is NOT swept clean, so the
consecutive count must restart. HuntNearby resets it via
``_reset_engagement_state``; without that reset a partial count (say 3) survived
the fight and the next single empty roam tripped the yield one sweep early,
dumping the warrior into the mining fallthrough.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import (
    WANDER_MAX_EMPTY,
    WanderForCombat,
    _reset_engagement_state,
)


@pytest.fixture(autouse=True)
def _no_real_sleep_or_clock(monkeypatch):
    # Mock asyncio.sleep and time.monotonic per harness rule (neither is reached
    # on these code paths, but pin them so the test can never block or drift).
    async def _instant(_s):
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _instant)
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1_000.0)


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


def test_reset_engagement_state_clears_wander_empty():
    ctx = _ctx()
    ctx.blackboard["_wander_empty"] = WANDER_MAX_EMPTY - 1  # mid-count, not yet yielding
    _reset_engagement_state(ctx)
    assert ctx.blackboard["_wander_empty"] == 0


@pytest.mark.asyncio
async def test_empty_count_does_not_leak_across_a_hunt(monkeypatch):
    """A partial empty-roam count cleared by a fight must NOT cause the next
    wander sweep to yield prematurely."""
    async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
        return None

    import anima.action.movement as movement
    monkeypatch.setattr(movement, "go_to", fake_go_to)

    ctx = _ctx()
    proc = WanderForCombat()
    # Build up a near-yield consecutive-empty count over several sweeps.
    for _ in range(WANDER_MAX_EMPTY - 1):
        r = await proc.execute(ctx)
        assert r.success is True  # still roaming, has not yielded yet
    assert ctx.blackboard["_wander_empty"] == WANDER_MAX_EMPTY - 1

    # A real fight happens (engagement start clears the per-engagement scratch).
    _reset_engagement_state(ctx)
    assert ctx.blackboard["_wander_empty"] == 0

    # The fight ends and wander resumes: a SINGLE empty roam must NOT yield —
    # the count was leaking before the fix and tripped WANDER_MAX_EMPTY at once.
    r = await proc.execute(ctx)
    assert r.success is True
    assert r.next_suggestion == "wander_for_combat"
    assert ctx.blackboard["_wander_empty"] == 1
