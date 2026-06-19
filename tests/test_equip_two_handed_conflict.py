"""A two-handed weapon needs BOTH hands free on ServUO.

BaseWeapon.CheckConflictingLayer rejects a two-hander while the one-handed
layer is occupied ("You already have something in both hands."), so the
EquipItem (0x13) is refused. ``equip_weapon_from_pack(two_handed=True)``
previously only checked the two-handed layer (2): it would PickUp the weapon
(lifting it loose) and then report a soft success on the rejected wear,
disarming the agent. It must instead refuse before lifting anything.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.actions.equip as equip
from anima.actions.equip import (
    LAYER_ONE_HANDED,
    LAYER_TWO_HANDED,
    equip_weapon_from_pack,
)

WEAPONS = {0x143A}  # a maul (mace-fighting weapon graphic)


def _ctx(equipment):
    ss = SimpleNamespace(serial=0x1, equipment=dict(equipment))
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        conn=SimpleNamespace(send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_two_handed_refused_when_one_hand_occupied(monkeypatch):
    # A one-handed weapon is already equipped (layer 1); the two-handed layer
    # is free. ServUO would reject the two-hander outright.
    ctx = _ctx({LAYER_ONE_HANDED: 0x111})
    lifted = AsyncMock(return_value=SimpleNamespace(success=True))
    # If the guard works we never reach a find/equip — assert by spying.
    monkeypatch.setattr(
        equip, "equip_item",
        AsyncMock(side_effect=AssertionError("must not equip when one hand full")),
    )
    monkeypatch.setattr(
        "anima.actions.inventory.find_in_backpack",
        lambda c, g: (_ for _ in ()).throw(
            AssertionError("must not even scan/lift a weapon")
        ),
    )
    monkeypatch.setattr(ctx.conn, "send_packet", lifted)

    result = await equip_weapon_from_pack(ctx, WEAPONS, two_handed=True)

    assert result.success is False
    # Nothing was lifted: no PickUp/Equip packets went out.
    lifted.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_handed_equips_when_both_hands_free(monkeypatch):
    # Both hands empty → the two-hander goes to layer 2 as before.
    ctx = _ctx({})
    weapon = SimpleNamespace(serial=0x222)
    monkeypatch.setattr(
        "anima.actions.inventory.find_in_backpack", lambda c, g: [weapon]
    )
    seen = {}

    async def fake_equip(c, serial, layer, timeout=2.0):
        seen["serial"], seen["layer"] = serial, layer
        return SimpleNamespace(success=True, data={"layer": layer, "serial": serial})

    monkeypatch.setattr(equip, "equip_item", fake_equip)

    result = await equip_weapon_from_pack(ctx, WEAPONS, two_handed=True)

    assert result.success is True
    assert seen == {"serial": 0x222, "layer": LAYER_TWO_HANDED}


@pytest.mark.asyncio
async def test_one_handed_unaffected_when_other_hand_full(monkeypatch):
    # A shield in the two-handed layer must NOT block a one-handed weapon.
    ctx = _ctx({LAYER_TWO_HANDED: 0x333})
    weapon = SimpleNamespace(serial=0x444)
    monkeypatch.setattr(
        "anima.actions.inventory.find_in_backpack", lambda c, g: [weapon]
    )
    seen = {}

    async def fake_equip(c, serial, layer, timeout=2.0):
        seen["serial"], seen["layer"] = serial, layer
        return SimpleNamespace(success=True, data={"layer": layer, "serial": serial})

    monkeypatch.setattr(equip, "equip_item", fake_equip)

    result = await equip_weapon_from_pack(ctx, WEAPONS, two_handed=False)

    assert result.success is True
    assert seen == {"serial": 0x444, "layer": LAYER_ONE_HANDED}
