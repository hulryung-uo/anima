"""A UO tool flips its graphic between held/ground/facing variants.

``_find_missing_tools`` must treat any variant in a tool's graphic family as
"already owned"; otherwise ``BuyFromNpc`` re-buys a tool it already carries
(e.g. a held hatchet shows as 0x0F47, not the canonical 0x0F43) and loops on
the purchase, burning gold.
"""

from unittest.mock import MagicMock

from anima.skills.trade.vendor import (
    ESSENTIAL_TOOLS,
    HATCHET_GRAPHICS,
    PICKAXE_GRAPHICS,
    SMITH_HAMMER_GRAPHICS,
    _find_missing_tools,
)

BACKPACK = 0x401


def _ctx(*backpack_graphics: int, equipped: dict[int, int] | None = None):
    """Mock ctx with the given graphics in the backpack (and optional gear)."""
    ctx = MagicMock()
    equipment = {0x15: BACKPACK}
    items: dict[int, MagicMock] = {}
    serial = 1
    for gfx in backpack_graphics:
        items[serial] = MagicMock(container=BACKPACK, graphic=gfx, serial=serial)
        serial += 1
    for layer, gfx in (equipped or {}).items():
        eq_serial = 0x1000 + layer
        equipment[layer] = eq_serial
        items[eq_serial] = MagicMock(container=0, graphic=gfx, serial=eq_serial)
    ctx.perception.self_state.equipment = equipment
    ctx.perception.world.items = items
    return ctx


def _missing_graphics(ctx) -> set[int]:
    return {g for g, _amt in _find_missing_tools(ctx)}


def test_empty_backpack_reports_all_essentials_missing():
    ctx = _ctx()
    missing = _missing_graphics(ctx)
    assert missing == {buy for buy, _amt, _fam in ESSENTIAL_TOOLS}


def test_noncanonical_hatchet_variant_counts_as_owned():
    """A held hatchet flips to 0x0F47 — it must NOT be reported missing."""
    variant = 0x0F47
    assert variant in HATCHET_GRAPHICS
    assert variant != 0x0F43  # not the canonical buy graphic
    ctx = _ctx(variant)
    missing = _missing_graphics(ctx)
    assert 0x0F43 not in missing  # the hatchet entry is satisfied


def test_alternate_pickaxe_graphic_counts_as_owned():
    alt = 0x0E85
    assert alt in PICKAXE_GRAPHICS
    assert alt != 0x0E86  # canonical pickaxe buy graphic
    ctx = _ctx(alt)
    assert 0x0E86 not in _missing_graphics(ctx)


def test_alternate_smith_hammer_graphic_counts_as_owned():
    alt = 0x13E4
    assert alt in SMITH_HAMMER_GRAPHICS
    assert alt != 0x13E3
    ctx = _ctx(alt)
    assert 0x13E3 not in _missing_graphics(ctx)


def test_equipped_hatchet_variant_counts_as_owned():
    """A hatchet wielded in a hand layer (0x01) counts too."""
    ctx = _ctx(equipped={0x01: 0x0F44})
    assert 0x0F44 in HATCHET_GRAPHICS
    assert 0x0F43 not in _missing_graphics(ctx)


def test_canonical_graphics_still_recognized():
    ctx = _ctx(0x0F43, 0x1034, 0x0E86, 0x13E3)
    assert _missing_graphics(ctx) == set()
