"""Keep-1 of a surplus tool group must retain the CHEAPEST unit.

A tool group spans multiple graphics (e.g. pickaxe = {0x0E85, 0x0E86}) and
the vendor pays a different price per graphic. When the agent has 2+ of a
group and must keep exactly one, keeping the first-listed unit (the old
behaviour) could retain the *highest*-priced duplicate and sell a cheaper
one — leaving gold on the table. We must keep the least-valuable unit so the
surplus we actually sell earns the most, while still keeping exactly one.
"""

from anima.perception.self_state import VendorSellItem
from anima.procedures.sell_to_vendor import SellToVendor

# A multi-graphic tool group, like pickaxe = {0x0E85, 0x0E86}.
_PICKAXE = {0x0E85, 0x0E86}


def _item(serial, graphic, price, amount=1, name="pick"):
    return VendorSellItem(
        serial=serial, graphic=graphic, amount=amount, price=price, name=name
    )


def test_keeps_cheapest_unit_of_multi_graphic_group():
    # First-listed unit is the EXPENSIVE one; a cheaper duplicate follows.
    sell_list = [
        _item(serial=1, graphic=0x0E85, price=8),   # expensive, listed first
        _item(serial=2, graphic=0x0E86, price=3),   # cheapest
    ]
    protected, relaxed = SellToVendor._pick_protected_serials(
        sell_list, [_PICKAXE]
    )
    # Keep exactly one — and it must be the cheapest (serial 2), NOT serial 1.
    assert protected == {2}
    assert relaxed == _PICKAXE


def test_keeps_cheapest_so_surplus_sale_maximizes_gold():
    sell_list = [
        _item(serial=1, graphic=0x0E85, price=8),   # expensive
        _item(serial=2, graphic=0x0E86, price=3),   # cheapest -> kept
        _item(serial=3, graphic=0x0E85, price=8),   # expensive
    ]
    protected, relaxed = SellToVendor._pick_protected_serials(
        sell_list, [_PICKAXE]
    )
    effective_keep = set(_PICKAXE) - relaxed
    sellable = SellToVendor._select_sellable(
        sell_list, effective_keep=effective_keep, protected_serials=protected,
    )
    sold_serials = {si.serial for si in sellable}
    # The two 8gp units are sold; the 3gp unit is the one we keep.
    assert sold_serials == {1, 3}
    assert sum(si.price * si.amount for si in sellable) == 16


def test_single_unit_group_stays_fully_protected():
    # Only one pickaxe -> no surplus, nothing relaxed, nothing protected.
    sell_list = [_item(serial=1, graphic=0x0E85, price=8)]
    protected, relaxed = SellToVendor._pick_protected_serials(
        sell_list, [_PICKAXE]
    )
    assert protected == set()
    assert relaxed == set()


def test_tie_on_price_keeps_smaller_stack():
    # Equal price -> keep the smaller stack so we sell the larger surplus.
    sell_list = [
        _item(serial=1, graphic=0x0E85, price=5, amount=3),
        _item(serial=2, graphic=0x0E86, price=5, amount=1),
    ]
    protected, _relaxed = SellToVendor._pick_protected_serials(
        sell_list, [_PICKAXE]
    )
    assert protected == {2}
