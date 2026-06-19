"""A shield in the off-hand must not block re-arming the empty weapon hand.

The Parrying stream equips a shield on layer 2 (the off-hand) and nothing in the
hunt loop ever takes it back off. If the agent then loses its weapon — disarmed,
looted into a corpse, or lost on death recovery — layer 1 (the weapon hand) goes
empty while layer 2 still holds the shield.

``HuntNearby.can_start`` still passes in that state (a weapon sits in the pack),
but ``execute`` used to gate the weapon equip on ``not get(1) and not get(2)`` —
i.e. it only re-armed when BOTH hands were empty. With a shield occupying layer
2, that guard was False, the whole equip block was skipped, and the agent fought
BARE-HANDED holding only a shield. Every swing then rolls Wrestling, never the
measured COMBAT stream (Swords/Tactics/Anatomy) — the exact disarm leak.

This pins the fix: a free weapon hand (layer 1) must be re-armed with a
one-handed weapon from the pack even when a shield already holds layer 2.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import ONE_HANDED_WEAPON_GRAPHICS, HuntNearby


def _hostile(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(pack_graphic, hostile, equipment):
    backpack_serial = 0x40000001
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        equipment=equipment, skills={},
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


@pytest.mark.asyncio
async def test_shield_in_offhand_still_arms_empty_weapon_hand(monkeypatch):
    # Layer 2 (off-hand) holds a shield from a prior engagement; layer 1 (the
    # weapon hand) is empty. A longsword sits in the pack. execute() MUST equip
    # the longsword into the weapon hand — not skip the equip and fight unarmed.
    backpack_serial = 0x40000001
    shield_serial = 0x60000000
    ctx = _ctx(
        pack_graphic=0x0F61,  # longsword (one-handed)
        hostile=_hostile(0x2, 103, 100),
        equipment={0x15: backpack_serial, 2: shield_serial},
    )
    ss = ctx.perception.self_state
    calls = []

    async def fake_equip(c, graphics, two_handed=False):
        calls.append((frozenset(graphics), two_handed))
        # Server wears the one-hander on layer 1 (off-hand shield stays put).
        ss.equipment[1] = 0x50000000
        return SimpleNamespace(success=True, data={"layer": 1, "serial": 0x50000000})

    # The shield path must NOT be attempted — layer 2 is already full.
    shield_called = AsyncMock(return_value=SimpleNamespace(success=False, data=None))
    monkeypatch.setattr(cl, "equip_weapon_from_pack", fake_equip)
    monkeypatch.setattr(cl, "equip_shield_from_pack", shield_called)

    result = await HuntNearby().execute(ctx)

    # The weapon hand was re-armed via the one-handed path despite the shield.
    assert calls == [(frozenset(ONE_HANDED_WEAPON_GRAPHICS), False)]
    assert ss.equipment.get(1) == 0x50000000
    # No two-handed fallback (the one-hander succeeded) and no double-shield.
    shield_called.assert_not_awaited()
    assert result.message != "No weapon to fight with"


@pytest.mark.asyncio
async def test_weapon_hand_full_skips_equip(monkeypatch):
    # Already armed in layer 1: no equip attempt at all (the original fast path).
    backpack_serial = 0x40000001
    ctx = _ctx(
        pack_graphic=0x0F61,
        hostile=_hostile(0x2, 103, 100),
        equipment={0x15: backpack_serial, 1: 0x70000000},
    )
    fake_equip = AsyncMock()
    monkeypatch.setattr(cl, "equip_weapon_from_pack", fake_equip)
    monkeypatch.setattr(
        cl, "equip_shield_from_pack",
        AsyncMock(return_value=SimpleNamespace(success=False, data=None)),
    )

    result = await HuntNearby().execute(ctx)

    fake_equip.assert_not_awaited()
    assert result.message != "No weapon to fight with"
