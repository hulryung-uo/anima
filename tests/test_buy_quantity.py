"""Tests for vendor buy-quantity bounding (anima.procedures.buy_from_vendor).

The quantity sent in the 0x3B BuyItems packet must be bounded by THREE
independent limits and floored at 1:

  1. restock need  (TOOL_MIN_STOCK minus what's already in the backpack)
  2. budget        (backpack + fresh bank gold)
  3. vendor stock  (VendorBuyItem.amount, the 0x3C/0x74 display count)

The stock cap is the regression under test: ServUO clamps the delivered
amount down to stock server-side but still lets the client *plan* a larger
quantity, which mis-logs cost and over-decrements the bank-balance cache.
"""

from unittest.mock import MagicMock

from anima.perception.self_state import VendorBuyItem
from anima.procedures.buy_from_vendor import (
    TOOL_MIN_STOCK,
    _buy_quantity,
)

PICKAXE = 0x0E86


def _mock_ctx(*backpack_graphics):
    """Mock AgentContext whose backpack holds the given tool graphics."""
    ctx = MagicMock()
    ctx.perception.self_state.equipment = {0x15: 0x101}
    items = {}
    for i, gfx in enumerate(backpack_graphics):
        items[i + 1] = MagicMock(
            container=0x101, graphic=gfx, amount=1, hue=0, serial=i + 1
        )
    ctx.perception.world.items = items
    return ctx


def _item(price: int, amount: int, graphic: int = PICKAXE) -> VendorBuyItem:
    return VendorBuyItem(
        serial=0xABCD, graphic=graphic, amount=amount, price=price, name="pickaxe"
    )


class TestBuyQuantity:
    def test_restock_need_with_ample_budget_and_stock(self):
        """Empty backpack, deep pockets, plenty of stock -> buy TOOL_MIN_STOCK."""
        ctx = _mock_ctx()  # 0 pickaxes
        qty = _buy_quantity(ctx, _item(price=11, amount=20), budget=10_000)
        assert qty == TOOL_MIN_STOCK

    def test_capped_by_budget(self):
        """Budget only covers 2 even though we'd like TOOL_MIN_STOCK."""
        ctx = _mock_ctx()
        # price 11, budget 25 -> 25 // 11 == 2
        qty = _buy_quantity(ctx, _item(price=11, amount=20), budget=25)
        assert qty == 2

    def test_capped_by_vendor_stock(self):
        """REGRESSION: vendor only has 2 in stock -> never plan more than 2.

        Before the fix, the quantity was bounded only by restock-need and
        budget, so an empty-backpack agent with gold would request
        TOOL_MIN_STOCK (3) even when the vendor displayed only 2. ServUO
        clamps delivery to 2 but the client still logged/charged for 3 and
        over-decremented its bank cache.
        """
        ctx = _mock_ctx()  # 0 pickaxes -> wants TOOL_MIN_STOCK (3)
        assert TOOL_MIN_STOCK > 2  # guard: the cap must actually bite
        qty = _buy_quantity(ctx, _item(price=11, amount=2), budget=10_000)
        assert qty == 2

    def test_stock_cap_is_the_binding_limit(self):
        """Stock 1 wins over a generous budget and full restock need."""
        ctx = _mock_ctx()
        qty = _buy_quantity(ctx, _item(price=11, amount=1), budget=10_000)
        assert qty == 1

    def test_never_returns_zero(self):
        """Even a barely-affordable single unit floors at 1, never 0."""
        ctx = _mock_ctx()
        # budget == price -> 11 // 11 == 1
        qty = _buy_quantity(ctx, _item(price=11, amount=5), budget=11)
        assert qty == 1

    def test_partial_existing_stock_reduces_restock_need(self):
        """Already holding some lowers how many we top up to TOOL_MIN_STOCK."""
        ctx = _mock_ctx(PICKAXE, PICKAXE)  # have 2 of TOOL_MIN_STOCK(3)
        qty = _buy_quantity(ctx, _item(price=11, amount=20), budget=10_000)
        assert qty == TOOL_MIN_STOCK - 2  # == 1
