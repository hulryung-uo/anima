"""The combat loot pass must bound ``_looted_corpses`` over a long soak.

``_loot_fresh_corpses`` adds a corpse serial to the shared ``_looted_corpses``
blackboard set every time it empties (or retires as empty) a body, and used to
never reap them. ServUO streams an unbounded number of DISTINCT corpse serials
over a soak — every Spawner mob dies, drops a fresh-serial corpse, then
despawns — so on a pure-combat profile (the planner's own ``_LootCorpses``
200-cap never runs) the set grew without bound for the whole session.

This drives thousands of distinct kills through the loot pass and asserts the
de-dup set stays bounded by ``LOOTED_CORPSES_CACHE_MAX``.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.procedures import combat_loop
from anima.procedures.combat_loop import (
    LOOTED_CORPSES_CACHE_MAX,
    _loot_fresh_corpses,
)


def _ctx():
    ss = SimpleNamespace(x=100, y=100)
    world = SimpleNamespace(items={})
    return SimpleNamespace(
        conn=SimpleNamespace(),
        blackboard={},
        perception=SimpleNamespace(self_state=ss, world=world),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(combat_loop.asyncio, "sleep", _instant)


def test_looted_corpses_set_stays_bounded_over_a_soak(monkeypatch):
    """Emptying thousands of distinct corpses (each a fresh serial, as a
    despawning Spawner mob produces) keeps ``_looted_corpses`` bounded instead
    of leaking one entry per kill for the whole session."""
    ctx = _ctx()

    # Each pass sees one brand-new corpse serial and lifts it to completion,
    # which retires the serial into ``_looted_corpses`` — the growth path.
    state = {"serial": 0}

    def _find(ctx, max_dist=2):
        state["serial"] += 1
        return [SimpleNamespace(serial=state["serial"], x=100, y=100, z=0)]

    monkeypatch.setattr(combat_loop, "find_corpses", _find)

    async def _loot(_ctx, _serial):
        return SimpleNamespace(
            success=True, message="Looted 1 items (10 gold)",
            data={"items": 1, "gold": 10},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    kills = LOOTED_CORPSES_CACHE_MAX * 5
    for _ in range(kills):
        asyncio.run(_loot_fresh_corpses(ctx))

    looted = ctx.blackboard["_looted_corpses"]
    # Without the cap this would equal ``kills``; the reap keeps it bounded.
    assert len(looted) <= LOOTED_CORPSES_CACHE_MAX
    assert kills > LOOTED_CORPSES_CACHE_MAX  # sanity: the soak really overflows


def test_recent_corpse_still_deduped_under_the_cap(monkeypatch):
    """Below the cap, a just-emptied corpse is still skipped on the next pass
    (the de-dup behavior the set exists for is preserved)."""
    ctx = _ctx()
    corpse = SimpleNamespace(serial=0xABCDEF, x=100, y=100, z=0)
    monkeypatch.setattr(combat_loop, "find_corpses", lambda c, max_dist=2: [corpse])

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        return SimpleNamespace(
            success=True, message="Looted 1 items (10 gold)",
            data={"items": 1, "gold": 10},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    assert asyncio.run(_loot_fresh_corpses(ctx)) == 10
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 0  # skipped, not re-opened
    assert calls["n"] == 1
