"""A weight-gated (partially-looted) corpse must stay eligible for re-looting.

When the pack fills mid-corpse, ``loot_corpse`` lifts what fits, then ``break``s
the lift loop with valuables (often gold) still in the corpse — a PARTIAL loot.
It still returns ``success=True`` (it did lift something). The old
``_loot_fresh_corpses`` added any successful corpse to ``_looted_corpses``
unconditionally, so the remaining gold/items were stranded forever: the next
post-kill pass — even after a sell/bank freed up weight — saw the serial marked
and skipped it. A direct, compounding gold-rate loss across a hunting session.

The fix: ``loot_corpse`` reports ``data["weight_gated"]`` when it stopped early
with valuables remaining, and ``_loot_fresh_corpses`` keeps such a corpse
eligible (does NOT retire it) while still retiring a corpse emptied to
completion.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.actions import loot
from anima.actions.loot import GOLD_GRAPHIC, WEIGHT_HEADROOM, loot_corpse
from anima.procedures import combat_loop
from anima.procedures.combat_loop import _loot_fresh_corpses

_BACKPACK = 0x40000015
_CORPSE = 0xABCDEF
_HEAVY = next(iter(loot._valuable_graphics()))


class _StubConn:
    async def send_packet(self, _pkt):  # noqa: D401 - stub
        return None


def _loot_ctx(items, *, weight, weight_max):
    ss = SimpleNamespace(
        equipment={loot.LAYER_BACKPACK: _BACKPACK},
        weight=weight,
        weight_max=weight_max,
    )
    world = SimpleNamespace(items={it.serial: it for it in items})
    return SimpleNamespace(
        conn=_StubConn(),
        perception=SimpleNamespace(self_state=ss, world=world),
    )


def _item(serial, graphic, amount=1):
    return SimpleNamespace(
        serial=serial, graphic=graphic, amount=amount, container=_CORPSE,
        x=0, y=0, z=0,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(loot.asyncio, "sleep", _instant)
    monkeypatch.setattr(combat_loop.asyncio, "sleep", _instant)


def test_loot_corpse_flags_weight_gated_partial_lift():
    # Budget allows exactly ONE lift (gold, sorted first) before the gate fires
    # on the heavy valuable that remains behind.
    weight_max = 100
    weight = weight_max - WEIGHT_HEADROOM - loot.WEIGHT_PER_ITEM_EST
    items = [
        _item(0x200, GOLD_GRAPHIC, amount=200),
        _item(0x100, _HEAVY),
    ]
    ctx = _loot_ctx(items, weight=weight, weight_max=weight_max)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    assert res.success is True
    assert res.data["gold"] == 200          # gold lifted
    assert res.data["items"] == 1           # heavy item left behind
    assert res.data["weight_gated"] is True  # partial — caller must not retire


def test_loot_corpse_complete_lift_not_weight_gated():
    # Plenty of headroom: everything is lifted, nothing remains.
    items = [
        _item(0x200, GOLD_GRAPHIC, amount=200),
        _item(0x100, _HEAVY),
    ]
    ctx = _loot_ctx(items, weight=0, weight_max=1000)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    assert res.success is True
    assert res.data["items"] == 2
    assert res.data["weight_gated"] is False


def _bb_ctx():
    ss = SimpleNamespace(x=100, y=100)
    world = SimpleNamespace(items={})
    return SimpleNamespace(
        conn=SimpleNamespace(),
        blackboard={},
        perception=SimpleNamespace(self_state=ss, world=world),
    )


def _corpse(serial=_CORPSE):
    return SimpleNamespace(serial=serial, x=100, y=100, z=0)


def test_weight_gated_corpse_is_retried_then_completed(monkeypatch):
    """A partial (weight-gated) lift keeps the corpse eligible; once weight
    frees up the next pass completes it and only then retires it."""
    ctx = _bb_ctx()
    corpse = _corpse()
    monkeypatch.setattr(
        combat_loop, "find_corpses", lambda ctx, max_dist=2: [corpse]
    )

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        if calls["n"] == 1:
            # Pack full mid-corpse: lifted the gold, heavy item left behind.
            return SimpleNamespace(
                success=True, message="Looted 1 items (200 gold)",
                data={"items": 1, "gold": 200, "weight_gated": True},
            )
        # Weight freed up (sold/banked): the remainder is now collected.
        return SimpleNamespace(
            success=True, message="Looted 1 items (0 gold)",
            data={"items": 1, "gold": 0, "weight_gated": False},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    # First pass: partial lift, corpse must stay eligible (the regression
    # retired it here and stranded the heavy item forever).
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 200
    assert corpse.serial not in ctx.blackboard["_looted_corpses"]

    # Second pass: same corpse is revisited and finished, then retired.
    asyncio.run(_loot_fresh_corpses(ctx))
    assert calls["n"] == 2
    assert corpse.serial in ctx.blackboard["_looted_corpses"]


def test_complete_lift_retires_corpse(monkeypatch):
    """A corpse emptied to completion is retired immediately (no re-open)."""
    ctx = _bb_ctx()
    corpse = _corpse()
    monkeypatch.setattr(
        combat_loop, "find_corpses", lambda ctx, max_dist=2: [corpse]
    )

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        return SimpleNamespace(
            success=True, message="Looted 2 items (100 gold)",
            data={"items": 2, "gold": 100, "weight_gated": False},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    assert asyncio.run(_loot_fresh_corpses(ctx)) == 100
    assert asyncio.run(_loot_fresh_corpses(ctx)) == 0  # skipped: retired
    assert calls["n"] == 1


def test_fully_weight_gated_corpse_never_retired(monkeypatch):
    """Pack so full even the gold can't be lifted: ``loot_corpse`` returns
    ``success=False`` with ``weight_gated=True`` and nothing lifted.

    Regression: that fell through to the empty-open branch, so after
    LOOT_MAX_ATTEMPTS such opens the corpse was retired into
    ``_looted_corpses`` forever — stranding its (untouched) gold the moment
    weight later freed up. A weight-gated open must NEVER count as an empty
    open, even when zero items were lifted.
    """
    ctx = _bb_ctx()
    corpse = _corpse()
    monkeypatch.setattr(
        combat_loop, "find_corpses", lambda ctx, max_dist=2: [corpse]
    )

    calls = {"n": 0}

    async def _loot(_ctx, _serial):
        calls["n"] += 1
        # Pack maxed out: gold (sorted first) itself won't fit, so the lift
        # loop skips everything. Nothing lifted, but the corpse is NOT empty.
        return SimpleNamespace(
            success=False, message="Corpse had nothing useful",
            data={"items": 0, "gold": 0, "weight_gated": True},
        )

    monkeypatch.setattr(combat_loop, "loot_corpse", _loot)

    # Open it far more times than LOOT_MAX_ATTEMPTS would retire on.
    for _ in range(combat_loop.LOOT_MAX_ATTEMPTS + 2):
        assert asyncio.run(_loot_fresh_corpses(ctx)) == 0

    # Never retired: it stays eligible so a future weight-freed pass collects it.
    assert corpse.serial not in ctx.blackboard["_looted_corpses"]
    # And it was never counted toward the empty-open retirement budget.
    assert corpse.serial not in ctx.blackboard["_loot_attempts"]
