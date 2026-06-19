"""A looted two-handed axe must be worn on the TWO-handed layer.

On ServUO an axe/polearm (BaseAxe / BasePoleArm) carries Layer.TwoHanded in its
tiledata, so the server equips it to layer 2 and BaseWeapon.CheckConflictingLayer
REFUSES any attempt to wear it on the one-handed layer ("You already have
something in both hands.", 500214). HuntNearby.execute previously called
``equip_weapon_from_pack(WEAPON_GRAPHICS)`` with the default ``two_handed=False``
— forcing every weapon, including the two-handed axes that commit b33c9e4 added
to WEAPON_GRAPHICS so a looted axe "arms combat", onto layer 1. The server
rejects that, the axe strands on the cursor, the agent fights unarmed and the
whole COMBAT skill stream (Swords/Tactics/Anatomy) never rolls.

This pins the fix: when the only pack weapon is two-handed, execute() must equip
it with ``two_handed=True`` (layer 2) and must NOT then try to add a shield on
top of it.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import (
    ONE_HANDED_WEAPON_GRAPHICS,
    TWO_HANDED_WEAPON_GRAPHICS,
    WEAPON_GRAPHICS,
    HuntNearby,
)


def _hostile(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(pack_graphic, hostile):
    backpack_serial = 0x40000001
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        equipment={0x15: backpack_serial}, skills={},
    )
    items = {
        0x50000000: SimpleNamespace(
            serial=0x50000000, graphic=pack_graphic,
            container=backpack_serial, amount=1,
        )
    }
    mobiles = {hostile.serial: hostile}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items=items)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=False, send_packet=AsyncMock()),
    )


def test_partition_is_complete_and_disjoint():
    # The two subsets must exactly tile WEAPON_GRAPHICS with no overlap, or a
    # weapon could be silently dropped from (or double-counted across) both
    # equip paths.
    assert TWO_HANDED_WEAPON_GRAPHICS <= WEAPON_GRAPHICS
    assert ONE_HANDED_WEAPON_GRAPHICS == WEAPON_GRAPHICS - TWO_HANDED_WEAPON_GRAPHICS
    assert not (ONE_HANDED_WEAPON_GRAPHICS & TWO_HANDED_WEAPON_GRAPHICS)
    assert ONE_HANDED_WEAPON_GRAPHICS | TWO_HANDED_WEAPON_GRAPHICS == WEAPON_GRAPHICS
    # A longsword is one-handed; a halberd/two-handed-axe is two-handed.
    assert 0x0F61 in ONE_HANDED_WEAPON_GRAPHICS
    assert 0x143E in TWO_HANDED_WEAPON_GRAPHICS
    assert 0x1443 in TWO_HANDED_WEAPON_GRAPHICS


@pytest.mark.asyncio
@pytest.mark.parametrize("axe", sorted(TWO_HANDED_WEAPON_GRAPHICS))
async def test_two_handed_axe_is_equipped_two_handed(axe, monkeypatch):
    # The pack holds only a two-handed axe. execute() must try the one-handed
    # set first (fails — no one-handed weapon), then equip the axe on layer 2.
    ctx = _ctx(pack_graphic=axe, hostile=_hostile(0x2, 103, 100))
    ss = ctx.perception.self_state
    calls = []

    async def fake_equip(c, graphics, two_handed=False):
        calls.append((frozenset(graphics), two_handed))
        # One-handed attempt finds nothing in a two-hander-only pack.
        if not two_handed:
            return SimpleNamespace(success=False, message="No weapon in backpack")
        # Two-handed attempt succeeds and fills layer 2 (as the server would).
        ss.equipment[2] = 0x50000000
        return SimpleNamespace(success=True, data={"layer": 2, "serial": 0x50000000})

    shield_called = AsyncMock()
    monkeypatch.setattr(cl, "equip_weapon_from_pack", fake_equip)
    monkeypatch.setattr(cl, "equip_shield_from_pack", shield_called)

    result = await HuntNearby().execute(ctx)

    # First a one-handed attempt, then a two-handed=True attempt with the
    # two-handed graphic set.
    assert calls[0][1] is False
    assert calls[1] == (frozenset(TWO_HANDED_WEAPON_GRAPHICS), True)
    # And NO shield was ever attempted over the two-hander (would strand it).
    shield_called.assert_not_awaited()
    # The hunt was not aborted for lack of a weapon.
    assert result.message != "No weapon to fight with"


@pytest.mark.asyncio
async def test_one_handed_weapon_still_takes_the_one_handed_path(monkeypatch):
    # A longsword (one-handed) must equip via the one-handed path, leaving the
    # off-hand free so the shield (Parrying stream) is still attempted.
    ctx = _ctx(pack_graphic=0x0F61, hostile=_hostile(0x2, 103, 100))
    calls = []

    async def fake_equip(c, graphics, two_handed=False):
        calls.append((frozenset(graphics), two_handed))
        return SimpleNamespace(success=True, data={"layer": 1, "serial": 0x50000000})

    shield_called = AsyncMock(return_value=SimpleNamespace(success=False, data=None))
    monkeypatch.setattr(cl, "equip_weapon_from_pack", fake_equip)
    monkeypatch.setattr(cl, "equip_shield_from_pack", shield_called)

    result = await HuntNearby().execute(ctx)

    # Only the one-handed attempt ran (it succeeded, no two-handed fallback).
    assert calls == [(frozenset(ONE_HANDED_WEAPON_GRAPHICS), False)]
    # The shield path WAS attempted (off-hand free on a one-hander).
    shield_called.assert_awaited()
    assert result.message != "No weapon to fight with"
