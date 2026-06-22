"""Surplus pickaxes/saws must route to a real vendor type, not None.

Regression: ITEM_VENDOR_MAP had no entry for PICKAXE_GRAPHICS or
SAW_GRAPHICS, so when sell_to_vendor relaxed KEEP protection for surplus
(count >= 2) tools it called ITEM_VENDOR_MAP.get(graphic) -> None and the
agent treated it as "any vendor", walked to the wrong shop, and got
refused.  ServUO SBTinker/SBBlacksmith/SBMiner sell-lists carry Pickaxe;
SBTinker carries Saw.  Map both to the vendor types that actually buy
them so the planner picks a correct shop.
"""

from anima.procedures.vendor_knowledge import (
    ITEM_VENDOR_MAP,
    get_vendor_keywords_for_items,
)
from anima.skills.crafting.tinker import (
    PICKAXE_GRAPHICS,
    SAW_GRAPHICS,
)


def test_every_pickaxe_graphic_routes_to_tinker_or_blacksmith():
    for g in PICKAXE_GRAPHICS:
        vendors = ITEM_VENDOR_MAP.get(g)
        # The whole point of the fix: NOT None ("any vendor").
        assert vendors is not None, f"pickaxe 0x{g:04X} unmapped -> any vendor"
        # Pickaxes are bought by tinker, blacksmith, and miner on ServUO.
        assert "tinker" in vendors
        assert "blacksmith" in vendors


def test_every_saw_graphic_routes_to_tinker():
    for g in SAW_GRAPHICS:
        vendors = ITEM_VENDOR_MAP.get(g)
        assert vendors is not None, f"saw 0x{g:04X} unmapped -> any vendor"
        # Only SBTinker carries Saw in its sell list.
        assert "tinker" in vendors


def test_get_vendor_keywords_for_surplus_pickaxes():
    # Mirrors _desired_vendor_types: a set of surplus pickaxe graphics
    # must resolve to concrete vendor keywords, never the empty/default
    # fallback that masks the routing.
    keywords = get_vendor_keywords_for_items(set(PICKAXE_GRAPHICS))
    assert "tinker" in keywords
    assert "blacksmith" in keywords


def test_single_pickaxe_graphic_stays_protected_as_keep():
    # A *single* pickaxe (no surplus) is never relaxed out of KEEP, so it
    # never contributes a vendor type — sell_to_vendor only maps a group
    # once it has 2+ members.  This documents the "single tool -> None"
    # contract at the procedure layer.
    from anima.perception.self_state import VendorSellItem
    from anima.procedures.sell_to_vendor import SellToVendor

    pickaxe_g = sorted(PICKAXE_GRAPHICS)[0]
    sell_list = [
        VendorSellItem(
            serial=1, graphic=pickaxe_g, amount=1, price=8, name="pickaxe"
        )
    ]
    protected, relaxed = SellToVendor._pick_protected_serials(
        sell_list, [set(PICKAXE_GRAPHICS)]
    )
    # Only one unit -> nothing relaxed, nothing offered for sale.
    assert protected == set()
    assert relaxed == set()
