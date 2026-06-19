"""An agent ALREADY wielding a two-handed weapon must not abort the hunt.

A two-handed weapon (axe / polearm) is worn on the TwoHanded layer (layer 2)
ONLY — layer 1 stays empty (ServUO reads the weapon's layer straight from its
tiledata; BaseAxe/BasePoleArm carry Layer.TwoHanded). So an agent that
re-equipped a looted two-hander between fights (post-res re-equip, commit
f57c7e3) reads as ``not ss.equipment.get(1)`` even though it is fully armed.

The bug this pins: HuntNearby.execute saw layer 1 empty and entered the equip
block. The one-handed attempt is correctly refused (a worn two-hander on layer 2
blocks a one-hander, ServUO BaseWeapon.CheckConflictingLayer message 500214),
and the two-handed FALLBACK is gated on ``not ss.equipment.get(2)`` — which the
worn two-hander makes false — so execute() fell straight through to
``return ... "No weapon to fight with"`` and ABORTED the hunt while the agent
held a perfectly good two-handed weapon. Every swing it could have landed (the
whole COMBAT skill stream) was lost.

The fix detects the worn two-hander up front, skips the equip block entirely
(so it never bails), and marks it as two-handed so the shield path below does
not try to add a shield on top of it.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import TWO_HANDED_WEAPON_GRAPHICS, HuntNearby


def _hostile(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(worn_two_hander_graphic, hostile):
    backpack_serial = 0x40000001
    worn_serial = 0x60000000
    # Layer 1 (one-handed) is EMPTY; layer 2 holds the worn two-hander. This is
    # exactly the worn-state of an agent that re-equipped a looted two-hander.
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        equipment={0x15: backpack_serial, 2: worn_serial}, skills={},
    )
    items = {
        worn_serial: SimpleNamespace(
            serial=worn_serial, graphic=worn_two_hander_graphic,
            container=None, amount=1,
        ),
    }
    mobiles = {hostile.serial: hostile}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items=items)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_worn_two_hander_does_not_abort_or_re_equip(monkeypatch):
    # Pick any two-handed graphic; the worn-detection is graphic-based.
    two_hander = sorted(TWO_HANDED_WEAPON_GRAPHICS)[0]
    ctx = _ctx(worn_two_hander_graphic=two_hander, hostile=_hostile(0x2, 101, 100))

    # asyncio.sleep → instant no-op; time.monotonic → jump past the engagement
    # deadline after it is first read so the swing loop exits after one short
    # pass (the equip decision under test all happens before the loop body).
    monkeypatch.setattr(cl.asyncio, "sleep", AsyncMock())
    ticks = iter([0.0, 0.0, 1e9, 1e9, 1e9, 1e9, 1e9, 1e9])
    last = [1e9]

    def fake_monotonic():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(cl.time, "monotonic", fake_monotonic)

    equip_weapon = AsyncMock()
    equip_shield = AsyncMock()
    monkeypatch.setattr(cl, "equip_weapon_from_pack", equip_weapon)
    monkeypatch.setattr(cl, "equip_shield_from_pack", equip_shield)

    result = await HuntNearby().execute(ctx)

    # The agent is already armed with a two-hander: NO equip attempt of any kind.
    equip_weapon.assert_not_awaited()
    # And NO shield attempt — a shield on top of the two-hander would be refused
    # by the server and strand the shield on the cursor.
    equip_shield.assert_not_awaited()
    # Crucially, the hunt was NOT aborted for lack of a weapon.
    assert result.message != "No weapon to fight with"
    # The worn two-hander is untouched on layer 2.
    assert ctx.perception.self_state.equipment[2] == 0x60000000


@pytest.mark.asyncio
async def test_empty_offhand_still_equips_from_pack(monkeypatch):
    # Control: with layer 2 EMPTY (no worn two-hander) the equip block still runs
    # as before — the fix must not suppress equipping for a genuinely-disarmed
    # agent.
    hostile = _hostile(0x2, 101, 100)
    ctx = _ctx(worn_two_hander_graphic=sorted(TWO_HANDED_WEAPON_GRAPHICS)[0], hostile=hostile)
    ctx.perception.self_state.equipment.pop(2)  # off-hand now free

    monkeypatch.setattr(cl.asyncio, "sleep", AsyncMock())
    ticks = iter([0.0, 0.0, 1e9, 1e9, 1e9, 1e9])
    last = [1e9]

    def fake_monotonic():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(cl.time, "monotonic", fake_monotonic)

    async def fake_equip(c, graphics, two_handed=False):
        # One-handed attempt succeeds (a longsword in the pack).
        c.perception.self_state.equipment[1] = 0x70000000
        return SimpleNamespace(success=True, data={"layer": 1, "serial": 0x70000000})

    equip_weapon = AsyncMock(side_effect=fake_equip)
    equip_shield = AsyncMock(return_value=SimpleNamespace(success=False, data=None))
    monkeypatch.setattr(cl, "equip_weapon_from_pack", equip_weapon)
    monkeypatch.setattr(cl, "equip_shield_from_pack", equip_shield)

    result = await HuntNearby().execute(ctx)

    equip_weapon.assert_awaited()  # the equip block ran for a disarmed agent
    assert result.message != "No weapon to fight with"
