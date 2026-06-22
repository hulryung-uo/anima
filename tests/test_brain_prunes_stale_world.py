"""Regression: Brain.tick must reap stale mobiles from a real WorldState.

``WorldState.prune_stale_mobiles`` existed (with unit tests) but had NO runtime
caller, so ``world.mobiles`` grew unbounded across a long eval — every mob that
wandered into view and left without a 0x1D Delete lingered forever, dragging its
worn-item / backpack subtree along. ``Brain._prune_stale_world`` now wires the
reaper into the one periodic hook every agent runs, throttled to the TTL cadence
and guarding the agent's own serial plus the actively-engaged combat target.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.brain.brain import PRUNE_INTERVAL_S, PRUNE_MAX_AGE_S, Brain
from anima.perception.world_state import WorldState

MY_SERIAL = 0x00000001
STALE_SERIAL = 0x00010000
FRESH_SERIAL = 0x00020000
ENGAGED_SERIAL = 0x00030000


class _NoopRoot:
    async def tick(self, ctx) -> None:  # noqa: ANN001 - test stub
        return None


def _make_brain(world: WorldState, *, blackboard: dict | None = None) -> Brain:
    self_state = SimpleNamespace(serial=MY_SERIAL)
    perception = SimpleNamespace(
        self_state=self_state,
        world=world,
        poll_events=lambda: [],
    )
    ctx = SimpleNamespace(perception=perception, blackboard=blackboard or {})
    return Brain(ctx, root=_NoopRoot())


def _seed(world: WorldState, serial: int, last_seen: float) -> None:
    mob = world.get_or_create_mobile(serial)
    mob.last_seen = last_seen


def test_tick_reaps_stale_but_keeps_fresh() -> None:
    world = WorldState()
    now = 1_000_000.0
    # Stale: well past the TTL. Fresh: just touched.
    _seed(world, STALE_SERIAL, now - (PRUNE_MAX_AGE_S + 60.0))
    _seed(world, FRESH_SERIAL, now - 1.0)
    brain = _make_brain(world)
    # Drive the prune at a controlled clock so we don't depend on wall time.
    brain._last_prune = now - (PRUNE_INTERVAL_S + 1.0)
    import anima.brain.brain as brain_mod

    orig = brain_mod.time.monotonic
    brain_mod.time.monotonic = lambda: now
    try:
        asyncio.run(brain.tick())
    finally:
        brain_mod.time.monotonic = orig

    assert STALE_SERIAL not in world.mobiles, "stale mobile must be reaped"
    assert FRESH_SERIAL in world.mobiles, "fresh mobile must survive"


def test_tick_never_reaps_engaged_target_or_self() -> None:
    world = WorldState()
    now = 1_000_000.0
    # Both self and the engaged target are stale by the clock, but must survive.
    _seed(world, MY_SERIAL, now - (PRUNE_MAX_AGE_S + 100.0))
    _seed(world, ENGAGED_SERIAL, now - (PRUNE_MAX_AGE_S + 100.0))
    _seed(world, STALE_SERIAL, now - (PRUNE_MAX_AGE_S + 100.0))
    brain = _make_brain(world, blackboard={"combat_target_serial": ENGAGED_SERIAL})
    brain._last_prune = now - (PRUNE_INTERVAL_S + 1.0)
    import anima.brain.brain as brain_mod

    orig = brain_mod.time.monotonic
    brain_mod.time.monotonic = lambda: now
    try:
        asyncio.run(brain.tick())
    finally:
        brain_mod.time.monotonic = orig

    assert MY_SERIAL in world.mobiles, "agent's own mobile must never be reaped"
    assert ENGAGED_SERIAL in world.mobiles, (
        "the actively-engaged combat target must never be reaped mid-fight"
    )
    assert STALE_SERIAL not in world.mobiles, "an unprotected stale mob still goes"


def test_prune_is_throttled_between_ticks() -> None:
    world = WorldState()
    now = 1_000_000.0
    _seed(world, STALE_SERIAL, now - (PRUNE_MAX_AGE_S + 100.0))
    brain = _make_brain(world)
    # A prune just ran; the next tick is well inside the throttle window.
    brain._last_prune = now - 1.0
    import anima.brain.brain as brain_mod

    orig = brain_mod.time.monotonic
    brain_mod.time.monotonic = lambda: now
    try:
        asyncio.run(brain.tick())
    finally:
        brain_mod.time.monotonic = orig

    assert STALE_SERIAL in world.mobiles, (
        "prune must be throttled — no reap within PRUNE_INTERVAL_S of the last"
    )


def test_prune_cascades_worn_item_subtree() -> None:
    # A reaped mobile's worn items (container == mobile serial) must go too —
    # the whole point of routing the prune through WorldState.remove().
    world = WorldState()
    now = 1_000_000.0
    _seed(world, STALE_SERIAL, now - (PRUNE_MAX_AGE_S + 100.0))
    worn = world.get_or_create_item(0x40050000)
    worn.container = STALE_SERIAL
    brain = _make_brain(world)
    brain._last_prune = now - (PRUNE_INTERVAL_S + 1.0)
    import anima.brain.brain as brain_mod

    orig = brain_mod.time.monotonic
    brain_mod.time.monotonic = lambda: now
    try:
        asyncio.run(brain.tick())
    finally:
        brain_mod.time.monotonic = orig

    assert STALE_SERIAL not in world.mobiles
    assert 0x40050000 not in world.items, "worn-item child must cascade-delete"
