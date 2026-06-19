"""Corpse-loot weight gate: an unaffordable HEAVY item must be SKIPPED, not
treated as a hard stop that abandons the rest of the corpse.

The lift loop sorts gold first, then lightest-to-heaviest. A large gold pile
(``_loot_weight_est`` makes 5000 coins ~50 stones) is therefore forced to the
front of the candidate list regardless of its weight. The old loop ``break``ed
the instant the projected post-lift weight crossed the headroom band — so a
heavy gold pile at index 0 stranded EVERY light, near-weightless valuable that
followed it. The fix ``continue``s past the unaffordable item and keeps lifting
the cheaper remainder that still fits, while still flagging the corpse as
weight-gated (partial) so the caller keeps it eligible for a retry.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.actions import loot
from anima.actions.loot import GOLD_GRAPHIC, WEIGHT_HEADROOM, loot_corpse

_BACKPACK = 0x40000015
_CORPSE = 0x12345
# A non-gold graphic the looter treats as valuable, resolved from the same
# source the implementation uses so the fixture stays in sync.
_VALUABLE = next(iter(loot._valuable_graphics()))


class _StubConn:
    def __init__(self):
        self.lifted: list[int] = []

    async def send_packet(self, _pkt):  # noqa: D401 - stub
        # Record the serial of every PickUp (0x07) so the test can assert
        # exactly which items were lifted.
        if _pkt and _pkt[0] == 0x07:
            self.lifted.append(int.from_bytes(_pkt[1:5], "big"))


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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(loot.asyncio, "sleep", _instant)


def test_heavy_gold_pile_does_not_strand_light_valuables():
    # Budget: room for a couple of light valuables, but NOT for the big gold
    # pile, which the gold-first sort forces to the front of the list.
    weight_max = 100
    weight = 40  # available = weight_max - HEADROOM - weight = 10 stones
    assert weight_max - WEIGHT_HEADROOM - weight == 10

    gold_serial = 0x900
    # 5000 coins -> ~50 stones via _loot_weight_est: way over the 10-stone
    # budget, so it can never be lifted on this pass.
    big_gold = _item(gold_serial, GOLD_GRAPHIC, amount=5000)
    assert loot._loot_weight_est(GOLD_GRAPHIC, 5000) > 10
    light = [_item(0x100 + i, _VALUABLE) for i in range(3)]
    ctx = _ctx([big_gold, *light], weight=weight, weight_max=weight_max)

    res = asyncio.run(loot_corpse(ctx, _CORPSE))

    lifted = ctx.conn.lifted
    # The unaffordable gold pile is skipped...
    assert gold_serial not in lifted, "heavy gold pile should not have been lifted"
    # ...but the light valuables behind it are recovered, not abandoned.
    assert {0x100, 0x101, 0x102} == set(lifted), (
        "light valuables behind the heavy gold pile must still be looted"
    )
    assert res.success is True
    assert res.data["items"] == 3
    assert res.data["gold"] == 0
    # The corpse is still partial (gold left behind) -> stays retry-eligible.
    assert res.data["weight_gated"] is True
