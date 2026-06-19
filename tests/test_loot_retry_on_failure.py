"""Post-kill corpse looting must RETRY a corpse whose first open paid nothing.

``loot_corpse`` returns ``success=False`` for two transient reasons: the
corpse's 0x3C contents stream hadn't arrived yet when we opened it, or the
weight gate broke before lifting anything because the pack was full. The old
``_loot_fresh_corpses`` added the corpse serial to ``_looted_corpses`` BEFORE
(and regardless of) the lift result, so a transient failure permanently
stranded that corpse's gold: every later kill's pass saw the serial already
marked and skipped it, even after a sell/bank freed up weight.

The fix marks a corpse looted only on a successful lift (or after a bounded
number of empty opens), so a corpse that paid nothing the first time is
retried on the next kill within loot range.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.procedures import combat_loop
from anima.procedures.combat_loop import LOOT_MAX_ATTEMPTS, _loot_fresh_corpses

_CORPSE = 0xABCDEF


def _ctx():
    ss = SimpleNamespace(x=100, y=100)
    world = SimpleNamespace(items={})
    return SimpleNamespace(
        conn=SimpleNamespace(),
        blackboard={},
        perception=SimpleNamespace(self_state=ss, world=world),
    )


def _corpse(serial=_CORPSE):
    return SimpleNamespace(serial=serial, x=100, y=100, z=0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(combat_loop.asyncio, "sleep", _instant)


def _patch_corpses(monkeypatch, corpses):
    monkeypatch.setattr(combat_loop, "find_corpses", lambda ctx, max_dist=2: corpses)


def test_failed_loot_is_retried_then_succeeds(monkeypatch):
    """A corpse that pays nothing on the first open (e.g. weight gate tripped
    on a full pack) is NOT retired — the next pass retries it and, once the
    pack frees up, recovers its gold."""
    ctx = _ctx()
    corpse = _corpse()
    _patch_corpses(monkeypatch, [corpse])

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        if calls["n"] == 1:
            # First open: transient failure (overweight gate / contents race).
            return SimpleNamespace(
                success=False, message="Corpse had nothing useful",
                data={"items": 0, "gold": 0},
            )
        # Second open after weight freed up: gold recovered.
        return SimpleNamespace(
            success=True, message="Looted 1 items (250 gold)",
            data={"items": 1, "gold": 250},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    # First kill's pass: open fails, no gold, corpse stays eligible.
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 0
    assert corpse.serial not in ctx.blackboard["_looted_corpses"]

    # Second kill's pass: same corpse is retried (the regression skipped it)
    # and the gold is recovered.
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 250
    assert calls["n"] == 2
    assert corpse.serial in ctx.blackboard["_looted_corpses"]


def test_successful_loot_is_not_reopened(monkeypatch):
    """A corpse looted successfully is retired immediately (no wasteful
    re-open on later passes)."""
    ctx = _ctx()
    corpse = _corpse()
    _patch_corpses(monkeypatch, [corpse])

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        return SimpleNamespace(
            success=True, message="Looted 1 items (100 gold)",
            data={"items": 1, "gold": 100},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    assert asyncio.run(_loot_fresh_corpses(ctx)) == 100
    # Second pass: serial already in _looted_corpses, so loot_corpse is NOT
    # called again.
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 0
    assert calls["n"] == 1


def test_persistently_empty_corpse_is_retired_after_cap(monkeypatch):
    """A corpse that keeps paying nothing is retired after LOOT_MAX_ATTEMPTS
    empty opens, so we don't re-open a truly-empty corpse on every kill."""
    ctx = _ctx()
    corpse = _corpse()
    _patch_corpses(monkeypatch, [corpse])

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        return SimpleNamespace(
            success=False, message="Corpse had nothing useful",
            data={"items": 0, "gold": 0},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    for _ in range(LOOT_MAX_ATTEMPTS):
        assert corpse.serial not in ctx.blackboard.get("_looted_corpses", set())
        assert asyncio.run(_loot_fresh_corpses(ctx)) == 0

    # After the cap, the corpse is retired and never re-opened.
    assert corpse.serial in ctx.blackboard["_looted_corpses"]
    n_after_cap = calls["n"]
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 0
    assert calls["n"] == n_after_cap
