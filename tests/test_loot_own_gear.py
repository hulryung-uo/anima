"""Death→ghost→resurrection→recover regression: the corpse-loot filter must
lift the agent's OWN combat gear back out of its corpse.

On death the wielded weapon drops into the corpse. The post-resurrection
recovery path (``planner._RecoverAfterDeath``) loots the corpse, then
re-equips via ``equip_weapon_from_pack(ctx, WEAPON_GRAPHICS)``. If
``loot_corpse`` does not lift a given weapon graphic, the weapon is left in
the corpse, the backpack stays empty, the re-equip finds nothing, and the
agent revives unarmed → immediate re-death.

The bug: most ``WEAPON_GRAPHICS`` (every mace-fighting weapon, plus several
sword/fencing variants) and two ``SHIELD_GRAPHICS`` were absent from
``ITEM_VENDOR_MAP``, so ``_valuable_graphics()`` did not cover them.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.actions.equip import SHIELD_GRAPHICS
from anima.actions.loot import GOLD_GRAPHIC, _valuable_graphics, loot_corpse
from anima.procedures.combat_loop import WEAPON_GRAPHICS

_BACKPACK = 0x40000015


def test_every_reequip_graphic_is_lootable():
    """Anything the re-equip path searches for must be liftable from a corpse.

    ``_RecoverAfterDeath`` re-equips using ``WEAPON_GRAPHICS`` (and a warrior
    may carry a shield), so every one of those graphics has to pass the
    ``loot_corpse`` valuable filter — otherwise it cannot be recovered.
    """
    valuable = _valuable_graphics()
    missing_weapons = WEAPON_GRAPHICS - valuable
    missing_shields = SHIELD_GRAPHICS - valuable
    assert not missing_weapons, (
        "weapon graphics not lootable from own corpse: "
        f"{sorted(hex(g) for g in missing_weapons)}"
    )
    assert not missing_shields, (
        "shield graphics not lootable from own corpse: "
        f"{sorted(hex(g) for g in missing_shields)}"
    )


def _corpse_ctx(corpse_serial, contents):
    """contents: list of (serial, graphic, amount) inside the corpse."""
    items = {}
    for serial, graphic, amount in contents:
        items[serial] = SimpleNamespace(
            serial=serial, container=corpse_serial, graphic=graphic, amount=amount,
        )
    ss = SimpleNamespace(
        equipment={0x15: _BACKPACK},
        weight=10,
        weight_max=400,
    )
    sent = []
    conn = SimpleNamespace(send_packet=AsyncMock(side_effect=lambda p: sent.append(p)))
    ctx = SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss, world=SimpleNamespace(items=items),
        ),
        conn=conn,
        blackboard={},
    )
    return ctx


@pytest.mark.asyncio
async def test_loot_corpse_lifts_mace_fighting_weapon(monkeypatch):
    """A mace-fighting warrior's maul (0x143A) — absent from the vendor map —
    must be lifted out of its own corpse so it can be re-equipped."""
    # No real network sleeps.
    import anima.actions.loot as loot_mod
    monkeypatch.setattr(loot_mod.asyncio, "sleep", AsyncMock())

    maul = 0x143A
    assert maul in WEAPON_GRAPHICS  # fixture guard: it's a real weapon graphic
    corpse_serial = 0x900
    ctx = _corpse_ctx(corpse_serial, [
        (0x101, maul, 1),            # the agent's own weapon
        (0x102, GOLD_GRAPHIC, 50),   # plus some gold
    ])
    result = await loot_corpse(ctx, corpse_serial)
    assert result.success is True
    # Both the weapon and the gold were lifted.
    assert result.data["items"] == 2
    assert result.data["gold"] == 50


@pytest.mark.asyncio
async def test_loot_corpse_skips_unrelated_junk(monkeypatch):
    """Sanity: the filter still rejects worthless graphics (no over-looting)."""
    import anima.actions.loot as loot_mod
    monkeypatch.setattr(loot_mod.asyncio, "sleep", AsyncMock())

    junk = 0x0EEE  # not gold, not a weapon/shield/tool/vendor item
    assert junk not in _valuable_graphics()
    corpse_serial = 0x900
    ctx = _corpse_ctx(corpse_serial, [(0x101, junk, 1)])
    result = await loot_corpse(ctx, corpse_serial)
    assert result.success is False
    assert result.data["items"] == 0
