"""A shield on the two-handed layer must not mask a failed two-hander equip.

ServUO wears a two-handed axe/polearm on layer 2 (TwoHanded). When a shield
already occupies that layer (the Parrying stream raises one and nothing takes
it off), the server refuses the two-handed weapon — it can never go on. The old
``equip_weapon_from_pack`` guard returned a blanket ``success=True,
"Hand already occupied"`` whenever the target layer held *anything*, so a shield
there produced a FALSE success: the caller believed the two-hander was wielded
while the agent kept fighting bare-handed behind the shield (every swing rolling
Wrestling, never the measured Swords/Tactics/Anatomy stream the COMBAT signal
depends on). This pins the fix: a non-matching item on the target layer is a
failure; only the requested weapon already being worn is a real success.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.actions.equip import (
    LAYER_TWO_HANDED,
    SHIELD_GRAPHICS,
    TWO_HANDED_WEAPON_GRAPHICS,
    equip_weapon_from_pack,
)


def _ctx(equipment, items):
    ss = SimpleNamespace(serial=0x1, equipment=equipment)
    world = SimpleNamespace(items=items)
    conn = SimpleNamespace(send_packet=AsyncMock())
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        conn=conn,
    )


@pytest.mark.asyncio
async def test_two_handed_blocked_by_shield_is_failure():
    shield_graphic = sorted(SHIELD_GRAPHICS)[0]
    shield_serial = 0x40000010
    backpack = 0x40000001
    axe_graphic = sorted(TWO_HANDED_WEAPON_GRAPHICS)[0]
    axe_serial = 0x50000000
    items = {
        shield_serial: SimpleNamespace(
            serial=shield_serial, graphic=shield_graphic,
            container=0, amount=1,
        ),
        axe_serial: SimpleNamespace(
            serial=axe_serial, graphic=axe_graphic,
            container=backpack, amount=1,
        ),
    }
    # Shield occupies the two-handed layer; the two-hander cannot go on.
    equipment = {0x15: backpack, LAYER_TWO_HANDED: shield_serial}
    ctx = _ctx(equipment, items)

    result = await equip_weapon_from_pack(
        ctx, TWO_HANDED_WEAPON_GRAPHICS, two_handed=True
    )

    # Not a false "Hand already occupied" success: the weapon was NOT equipped.
    assert result.success is False
    # And we never lifted the axe loose onto the cursor.
    ctx.conn.send_packet.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_holding_requested_two_hander_is_success():
    axe_graphic = sorted(TWO_HANDED_WEAPON_GRAPHICS)[0]
    axe_serial = 0x50000005
    items = {
        axe_serial: SimpleNamespace(
            serial=axe_serial, graphic=axe_graphic,
            container=0, amount=1,
        ),
    }
    equipment = {0x15: 0x40000001, LAYER_TWO_HANDED: axe_serial}
    ctx = _ctx(equipment, items)

    result = await equip_weapon_from_pack(
        ctx, TWO_HANDED_WEAPON_GRAPHICS, two_handed=True
    )
    # Already wearing the requested two-hander: real success, nothing to do.
    assert result.success is True
    ctx.conn.send_packet.assert_not_awaited()
