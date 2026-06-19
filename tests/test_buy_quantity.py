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
    _spendable_per_source,
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


def _mock_funds_ctx(backpack_gold: int, bank_gold: int):
    """Mock ctx with a given backpack gold and a fresh bank-balance cache."""
    import time

    ctx = MagicMock()
    ctx.perception.self_state.gold = backpack_gold
    ctx.blackboard = {"bank_balance": {"amount": bank_gold, "ts": time.time()}}
    return ctx


class TestSpendablePerSource:
    """The quantity budget must be the better single pocket, never the sum.

    REGRESSION: ServUO charges the full order from ONE source (backpack
    first, else a single bank withdraw) and delivers nothing if neither
    pocket alone covers it. Planning buy_qty against backpack+bank could
    request an order only the *combined* funds afford, which the server
    silently rejects — then the procedure trips a 10-minute buy-disable
    cooldown and the tool-restock loop starves.
    """

    def test_uses_larger_single_pocket_not_sum(self):
        # Split funds: neither pocket alone covers 3*11=33, but the sum
        # (60+60=120) would have. The budget must be the larger pocket (60).
        ctx = _mock_funds_ctx(backpack_gold=60, bank_gold=60)
        assert _spendable_per_source(ctx) == 60

    def test_plan_does_not_exceed_what_one_pocket_affords(self):
        # backpack=60, bank=60, price=11. Combined budget (120) would plan
        # 3 (cost 33) — but here the binding check is that we never plan an
        # order a single pocket cannot pay. With a deeper bank, the bank
        # alone funds the full restock.
        ctx = _mock_funds_ctx(backpack_gold=5, bank_gold=200)
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}  # empty backpack -> wants TOOL_MIN_STOCK
        budget = _spendable_per_source(ctx)  # == 200 (bank covers it)
        qty = _buy_quantity(ctx, _item(price=11, amount=20), budget=budget)
        assert qty == TOOL_MIN_STOCK
        assert qty * 11 <= budget  # the order fits one pocket

    def test_neither_pocket_covers_full_restock_falls_to_what_one_affords(self):
        # backpack=22, bank=22 (sum 44 >= 33) but each pocket only funds 2.
        ctx = _mock_funds_ctx(backpack_gold=22, bank_gold=22)
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}
        budget = _spendable_per_source(ctx)  # == 22 -> 22 // 11 == 2
        qty = _buy_quantity(ctx, _item(price=11, amount=20), budget=budget)
        assert qty == 2
        assert qty * 11 <= 22  # affordable from a single pocket
