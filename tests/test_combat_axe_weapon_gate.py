"""A looted axe must arm combat initiation — axes are Swordsmanship weapons.

``HuntNearby.can_start`` (and ``WanderForCombat.can_start``) gate on
``has_weapon and a hostile in range``, where ``has_weapon`` is satisfied by an
equipped weapon OR a weapon graphic in the backpack matching ``WEAPON_GRAPHICS``.
Axes are Swordsmanship weapons in UO/ServUO and one of the most common weapon
drops, yet the set previously listed only swords/fencing/mace-fighting graphics.
A warrior holding only a looted axe therefore read as unarmed: ``can_start``
returned False and the adventurer loop fell straight through to mining instead
of fighting. This pins the fix: an axe in the pack arms ``can_start``.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import WEAPON_GRAPHICS, HuntNearby

# ServUO axe item graphics (Swordsmanship). Mirrors vendor_knowledge._AXES.
AXE_GRAPHICS = {0x0F43, 0x0F45, 0x0F47, 0x0F49, 0x0F4B, 0x13AF, 0x13FA, 0x1443}


def _hostile(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(*, pack_graphics, hostiles, equipped_weapon=False):
    """Build an AgentContext mock with a backpack holding ``pack_graphics``.

    ``find_in_backpack`` scans ``world.items`` for items whose ``container``
    equals the serial under equipment layer 0x15 (BACKPACK), so we place an
    item per graphic inside a backpack container.
    """
    backpack_serial = 0x40000001
    equipment = {0x15: backpack_serial}
    if equipped_weapon:
        equipment[1] = 0xDEAD  # one-handed layer occupied

    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        equipment=equipment, skills={},
    )

    items = {}
    for i, g in enumerate(pack_graphics):
        serial = 0x50000000 + i
        items[serial] = SimpleNamespace(
            serial=serial, graphic=g, container=backpack_serial, amount=1,
        )

    mobiles = {m.serial: m for m in hostiles}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items=items)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def test_axe_family_is_in_the_weapon_set():
    """Every common axe graphic must be a recognized melee weapon."""
    missing = AXE_GRAPHICS - WEAPON_GRAPHICS
    assert not missing, f"axe graphics missing from WEAPON_GRAPHICS: " \
        f"{[hex(g) for g in sorted(missing)]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("axe", sorted(AXE_GRAPHICS))
async def test_can_start_with_only_a_looted_axe(axe):
    # No equipped weapon; the only thing in the pack is a single axe, and a
    # hostile is well within ENGAGE_RANGE. The warrior must be able to start
    # the fight rather than read as unarmed and fall through to mining.
    ctx = _ctx(pack_graphics={axe}, hostiles=[_hostile(0x2, 103, 100)])
    assert await HuntNearby().can_start(ctx) is True


@pytest.mark.asyncio
async def test_no_weapon_still_cannot_start():
    # Regression guard: an empty pack (no weapon graphic at all) must still
    # gate combat off even with a hostile in range — the fix widens the
    # recognized weapon set, it must not make an unarmed agent fight.
    ctx = _ctx(pack_graphics=set(), hostiles=[_hostile(0x2, 103, 100)])
    assert await HuntNearby().can_start(ctx) is False
