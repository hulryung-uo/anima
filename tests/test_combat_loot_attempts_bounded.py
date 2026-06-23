"""The combat loot pass must bound ``_loot_attempts`` over a long soak.

``_loot_fresh_corpses`` records an empty-open retry counter (keyed by corpse
serial) in the shared ``_loot_attempts`` blackboard dict. It only pops the
entry on a successful lift, a weight-gated open, or once the corpse reaches
LOOT_MAX_ATTEMPTS empty opens. A corpse that opens empty ONCE and then despawns
(the common case on a pure-combat soak — every Spawner mob drops a fresh-serial
corpse and despawns) is never popped, never recurs, and leaks one dict entry
per kill for the whole session. The sibling ``_looted_corpses`` set already has
a reaper; this guards that the retry counter is bounded the same way.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.procedures import combat_loop
from anima.procedures.combat_loop import (
    LOOT_ATTEMPTS_CACHE_MAX,
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


def test_loot_attempts_dict_stays_bounded_over_a_soak(monkeypatch):
    """A corpse that opens EMPTY once then despawns leaves a permanent
    ``_loot_attempts`` entry (count below LOOT_MAX_ATTEMPTS, serial never
    recurs). Driving thousands of such kills must keep the dict bounded
    instead of leaking one entry per kill for the whole session."""
    ctx = _ctx()

    # Each pass sees one brand-new corpse serial. The open returns empty
    # (success=False, not weight-gated) so the serial is counted once and,
    # because the next pass shows a DIFFERENT serial (the body despawned),
    # never reaches LOOT_MAX_ATTEMPTS and is never popped — the leak path.
    state = {"serial": 0}

    def _find(ctx, max_dist=2):
        state["serial"] += 1
        return [SimpleNamespace(serial=state["serial"], x=100, y=100, z=0)]

    monkeypatch.setattr(combat_loop, "find_corpses", _find)

    async def _loot(_ctx, _serial):
        return SimpleNamespace(
            success=False, message="empty",
            data={"items": 0, "gold": 0},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    kills = LOOT_ATTEMPTS_CACHE_MAX * 5
    for _ in range(kills):
        asyncio.run(_loot_fresh_corpses(ctx))

    attempts = ctx.blackboard["_loot_attempts"]
    # Without the cap this would equal ``kills``; the reap keeps it bounded.
    assert len(attempts) <= LOOT_ATTEMPTS_CACHE_MAX
    assert kills > LOOT_ATTEMPTS_CACHE_MAX  # sanity: the soak really overflows


def test_empty_corpse_still_retried_up_to_the_limit(monkeypatch):
    """Below the cap, the retry counter still works: the SAME empty corpse is
    re-opened until LOOT_MAX_ATTEMPTS, then retired into ``_looted_corpses``
    (the behavior the counter exists for is preserved)."""
    ctx = _ctx()
    corpse = SimpleNamespace(serial=0xABCDEF, x=100, y=100, z=0)
    monkeypatch.setattr(combat_loop, "find_corpses", lambda c, max_dist=2: [corpse])

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        return SimpleNamespace(
            success=False, message="empty",
            data={"items": 0, "gold": 0},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    # Re-open the same empty corpse until it retires after LOOT_MAX_ATTEMPTS.
    for _ in range(combat_loop.LOOT_MAX_ATTEMPTS):
        asyncio.run(_loot_fresh_corpses(ctx))
    assert calls["n"] == combat_loop.LOOT_MAX_ATTEMPTS
    # Now retired: popped from attempts and parked in the looted set, so the
    # next pass skips it (no further open).
    assert corpse.serial not in ctx.blackboard["_loot_attempts"]
    assert corpse.serial in ctx.blackboard["_looted_corpses"]
    asyncio.run(_loot_fresh_corpses(ctx))
    assert calls["n"] == combat_loop.LOOT_MAX_ATTEMPTS  # skipped, not re-opened
