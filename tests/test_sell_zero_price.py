"""SellToVendor must never put price-0 items in the 0x9F sell request.

ServUO's 0x9E sell list is built from ``IShopSellInfo.IsSellable`` only and
is not filtered by sell price (BaseVendor.cs ~L1154), so it routinely lists
items the vendor values at 0gp. ServUO's OnVendorSell still consumes any item
we send for it (delete / dump to BuyPack) while adding ``singlePrice * amount``
== 0 gold (BaseVendor.cs ~L2196-2207). Selling a 0-price item therefore
destroys the agent's item for nothing, so the selection must drop it.
"""

from anima.perception.self_state import VendorSellItem
from anima.procedures.sell_to_vendor import SellToVendor


def _item(serial, graphic, price, amount=1, name="x"):
    return VendorSellItem(
        serial=serial, graphic=graphic, amount=amount, price=price, name=name
    )


def test_zero_price_items_are_not_sold():
    sell_list = [
        _item(serial=1, graphic=0x1000, price=0, amount=5),   # worthless
        _item(serial=2, graphic=0x1001, price=12, amount=2),  # 24gp
        _item(serial=3, graphic=0x1002, price=0, amount=1),   # worthless
    ]
    sellable = SellToVendor._select_sellable(
        sell_list, effective_keep=set(), protected_serials=set()
    )
    serials = [si.serial for si in sellable]
    assert serials == [2], "only the priced item should be sold"
    assert all(si.price > 0 for si in sellable)


def test_priced_items_sorted_high_value_first():
    sell_list = [
        _item(serial=1, graphic=0x1000, price=5, amount=2),   # 10gp
        _item(serial=2, graphic=0x1001, price=50, amount=1),  # 50gp
        _item(serial=3, graphic=0x1002, price=3, amount=10),  # 30gp
    ]
    sellable = SellToVendor._select_sellable(
        sell_list, effective_keep=set(), protected_serials=set()
    )
    assert [si.serial for si in sellable] == [2, 3, 1]


def test_keep_and_protected_still_respected():
    sell_list = [
        _item(serial=1, graphic=0xAAAA, price=10, amount=1),  # kept by graphic
        _item(serial=2, graphic=0xBBBB, price=10, amount=1),  # protected serial
        _item(serial=3, graphic=0xCCCC, price=10, amount=1),  # sellable
    ]
    sellable = SellToVendor._select_sellable(
        sell_list,
        effective_keep={0xAAAA},
        protected_serials={2},
    )
    assert [si.serial for si in sellable] == [3]


def test_all_zero_price_yields_empty_selection():
    sell_list = [
        _item(serial=1, graphic=0x1000, price=0, amount=3),
        _item(serial=2, graphic=0x1001, price=0, amount=1),
    ]
    sellable = SellToVendor._select_sellable(
        sell_list, effective_keep=set(), protected_serials=set()
    )
    assert sellable == []
