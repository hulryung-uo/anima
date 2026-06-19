"""Corpse-loot weight gate: the lift loop must stop BEFORE it over-loots.

``ss.weight`` only refreshes when the server echoes a 0x11 status packet,
which lags far behind the ~0.6s-per-item lift loop in ``loot_corpse``. The
old gate read that stale weight for the whole corpse, so a single corpse
full of items would be lifted in one burst and push the agent into the
overweight (movement-blocked) state. The fix projects post-lift weight
locally and stops once the projection crosses the headroom band.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.actions import loot
from anima.actions.loot import (
    GOLD_GRAPHIC,
    WEIGHT_HEADROOM,
    WEIGHT_PER_ITEM_EST,
    loot_corpse,
)

_BACKPACK = 0x40000015
_CORPSE = 0x12345
# A non-gold graphic the looter treats as valuable, resolved from the same
# source the implementation uses so the fixture stays in sync.
_VALUABLE = next(iter(loot._valuable_graphics()))


class _StubConn:
    def __init__(self):
        self.sent = 0

    async def send_packet(self, _pkt):  # noqa: D401 - stub
        self.sent += 1


def _ctx(items, *, weight, weight_max):
    """Build a ctx whose ss.weight stays FIXED (simulating the lazy
    server-side 0x11 update that never arrives mid-loop)."""
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


def test_stops_before_overweight_despite_stale_weight():
    # Headroom budget: room for only a couple of items before the gate
    # should fire. weight_max - HEADROOM - weight = available stones.
    weight_max = 100
    weight = weight_max - WEIGHT_HEADROOM - 2 * WEIGHT_PER_ITEM_EST
    # Far more valuable items in the corpse than the budget allows.
    items = [_item(0x100 + i, _VALUABLE) for i in range(20)]
    ctx = _ctx(items, weight=weight, weight_max=weight_max)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    # The gate must stop well short of lifting all 20 items: the projection
    # only has room for ~2 lifts before crossing the headroom band.
    assert res.data["items"] <= 2
    assert res.data["items"] >= 1


def test_old_stale_gate_would_have_over_looted():
    # Document the regression: under the OLD gate (ss.weight only, never
    # advanced) the stale weight stays just under the limit forever, so the
    # loop would lift EVERY item. The new gate must not.
    weight_max = 100
    weight = weight_max - WEIGHT_HEADROOM - 2 * WEIGHT_PER_ITEM_EST
    items = [_item(0x100 + i, _VALUABLE) for i in range(20)]

    # Old behaviour reference: stale gate `ss.weight > weight_max - HEADROOM`
    # is False the whole time, so it never breaks.
    assert not (weight > weight_max - WEIGHT_HEADROOM)

    ctx = _ctx(items, weight=weight, weight_max=weight_max)
    res = asyncio.run(loot_corpse(ctx, _CORPSE))
    assert res.data["items"] < len(items)


def test_gold_amount_drives_projection():
    # A single big gold pile weighs ~amount/100 stones; the gate must count
    # that weight, not treat the pile as one flat item.
    weight_max = 100
    weight = 0
    # 5000 gold ~= 50 stones; with weight 0 and budget (100-50)=50 there is
    # room (50 + 50est? no): 50 stones gold est, limit 50 -> 0+50 > 50 false,
    # lifts it. A second 5000 pile would project 50+50 > 50 -> blocked.
    items = [
        _item(0x200, GOLD_GRAPHIC, amount=5000),
        _item(0x201, GOLD_GRAPHIC, amount=5000),
    ]
    ctx = _ctx(items, weight=weight, weight_max=weight_max)
    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    # Only the first pile fits; the projected weight from it blocks the rest.
    assert res.data["items"] == 1
    assert res.data["gold"] == 5000


def test_no_weight_max_does_not_gate():
    # weight_max == 0 (never received a status packet) → no gating, lift all.
    items = [_item(0x300 + i, _VALUABLE) for i in range(5)]
    ctx = _ctx(items, weight=0, weight_max=0)
    res = asyncio.run(loot_corpse(ctx, _CORPSE))
    assert res.data["items"] == 5
