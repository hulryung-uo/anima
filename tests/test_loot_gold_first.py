"""Corpse-loot ordering: gold must be lifted before heavy valuables.

The weight gate ``break``s the lift loop the moment the next projected lift
would cross the headroom band, abandoning the rest of the corpse. If a heavy
ore/ingot iterates BEFORE the gold pile (raw dict order is arbitrary), the
gate fires on the heavy item and the gold — near-weightless and the most
valuable thing on the corpse — is stranded. ``loot_corpse`` must sort gold
first so it is never sacrificed for a heavy item.
"""
import asyncio
from types import SimpleNamespace

from anima.actions import loot
from anima.actions.loot import (
    GOLD_GRAPHIC,
    WEIGHT_HEADROOM,
    loot_corpse,
)

_BACKPACK = 0x40000015
_CORPSE = 0x12345
# A heavy non-gold valuable the looter will lift (resolved from the same
# source the implementation uses so the fixture stays in sync).
_HEAVY = next(iter(loot._valuable_graphics()))


class _StubConn:
    def __init__(self):
        self.sent = 0

    async def send_packet(self, _pkt):  # noqa: D401 - stub
        self.sent += 1


def _ctx(items, *, weight, weight_max):
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


def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(loot.asyncio, "sleep", _instant)


def test_gold_lifted_before_heavy_item_strands_it(monkeypatch):
    _no_sleep(monkeypatch)
    # Budget allows exactly ONE lift before the gate fires: a heavy item is
    # ~WEIGHT_PER_ITEM_EST stones; a second lift would cross the band.
    weight_max = 100
    weight = weight_max - WEIGHT_HEADROOM - loot.WEIGHT_PER_ITEM_EST
    # The heavy valuable is inserted FIRST (arbitrary dict order would lift
    # it first under the old code and break before reaching the gold).
    items = [
        _item(0x100, _HEAVY),
        _item(0x200, GOLD_GRAPHIC, amount=200),
    ]
    ctx = _ctx(items, weight=weight, weight_max=weight_max)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    # With only room for one lift, gold (sorted first) must be the one taken
    # — the gold is never stranded behind the heavy item.
    assert res.data["gold"] == 200
    assert res.data["items"] == 1


def test_all_fit_lifts_everything(monkeypatch):
    _no_sleep(monkeypatch)
    # Plenty of headroom: ordering does not drop anything.
    weight_max = 1000
    items = [
        _item(0x100, _HEAVY),
        _item(0x200, GOLD_GRAPHIC, amount=500),
        _item(0x300, _HEAVY),
    ]
    ctx = _ctx(items, weight=0, weight_max=weight_max)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    assert res.data["items"] == 3
    assert res.data["gold"] == 500
