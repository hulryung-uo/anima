"""Regression: the bank trip and the deposit must agree on WHICH heavy items
count as depositable, not just on the weight threshold.

``DEPOSIT_GRAPHICS`` includes the iron-ingot graphics, but ``execute`` keeps
iron ingots (needed for crafting). Before the fix, ``can_start.has_heavy``
counted any DEPOSIT_GRAPHICS item, so an overweight miner carrying ONLY iron
ingots (no gold, no colored ingots, no ore/logs/boards) passed ``can_start``
but ``execute`` deposited nothing — a permanent no-op bank loop that never
lowered the weight that triggered it. ``_is_heavy_depositable`` is the single
predicate both sites now share.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.bank_deposit import (
    BankDeposit,
    _is_heavy_depositable,
)
from anima.skills.crafting.smelt import INGOT_GRAPHICS
from anima.skills.trade.banking import KEEP_GRAPHICS

IRON_INGOT = next(iter(INGOT_GRAPHICS))
COLORED_INGOT_HUE = 0x973  # any non-iron hue marker (hue field, not graphic)
ORE_GRAPHIC = 0x19B7       # in DEPOSIT_GRAPHICS, not an ingot
SHOVEL = 0x0F39            # in KEEP_GRAPHICS


def _item(serial, graphic, *, container, hue=0, amount=1):
    return SimpleNamespace(
        serial=serial, graphic=graphic, container=container, hue=hue, amount=amount
    )


# --- unit-level: the shared predicate -------------------------------------

def test_iron_ingot_is_not_heavy_depositable():
    assert _is_heavy_depositable(_item(1, IRON_INGOT, container=9, hue=0)) is False


def test_colored_ingot_graphic_is_depositable_when_not_iron_hue():
    # Colored ingots share an INGOT graphic but a non-iron hue — execute's
    # heavy loop would bank these, so the predicate must agree.
    assert _is_heavy_depositable(
        _item(1, IRON_INGOT, container=9, hue=COLORED_INGOT_HUE)
    ) is True


def test_ore_is_heavy_depositable():
    assert _is_heavy_depositable(_item(1, ORE_GRAPHIC, container=9)) is True


def test_kept_tool_is_not_heavy_depositable():
    assert SHOVEL in KEEP_GRAPHICS
    assert _is_heavy_depositable(_item(1, SHOVEL, container=9)) is False


# --- the regression: can_start must not promise a deposit execute won't make -

def _ctx_with_backpack_items(items, *, gold=0, weight=80, weight_max=100):
    backpack_serial = 0x40000001
    world = SimpleNamespace(items={it.serial: it for it in items})
    ss = SimpleNamespace(
        gold=gold,
        weight=weight,
        weight_max=weight_max,
        equipment={0x15: backpack_serial},
    )
    ctx = MagicMock()
    ctx.perception = SimpleNamespace(self_state=ss, world=world)
    ctx.blackboard = {}
    return ctx, backpack_serial


@pytest.mark.asyncio
async def test_can_start_false_for_iron_only_overweight():
    """The bug: overweight, only iron ingots, no gold/colored/ore.

    can_start must NOT trigger the trip, because execute would deposit nothing.
    """
    bp = 0x40000001
    items = [
        _item(1, IRON_INGOT, container=bp, hue=0, amount=50),
        _item(2, IRON_INGOT, container=bp, hue=0, amount=20),
    ]
    ctx, _ = _ctx_with_backpack_items(items, gold=0, weight=80, weight_max=100)
    proc = BankDeposit()
    with patch(
        "anima.procedures.bank_deposit._find_banker", return_value=object()
    ):
        assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_true_when_real_heavy_material_present():
    """An overweight miner carrying actual ore still makes the trip."""
    bp = 0x40000001
    items = [
        _item(1, IRON_INGOT, container=bp, hue=0, amount=50),  # kept
        _item(2, ORE_GRAPHIC, container=bp, amount=30),        # depositable
    ]
    ctx, _ = _ctx_with_backpack_items(items, gold=0, weight=80, weight_max=100)
    proc = BankDeposit()
    with patch(
        "anima.procedures.bank_deposit._find_banker", return_value=object()
    ):
        assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_iron_only_can_start_and_execute_agree():
    """End-to-end: can_start says no, and a forced execute would also bank
    nothing — proving the two sites are now in lockstep for iron-only loads."""
    bp = 0x40000001
    items = [_item(1, IRON_INGOT, container=bp, hue=0, amount=50)]
    ctx, bank = _ctx_with_backpack_items(items, gold=0, weight=80, weight_max=100)
    proc = BankDeposit()

    # can_start declines the trip
    with patch(
        "anima.procedures.bank_deposit._find_banker", return_value=object()
    ):
        assert await proc.can_start(ctx) is False

    # And if it HAD been started, execute deposits nothing (mock the world):
    banker = SimpleNamespace(serial=0x1234)
    ctx.conn = SimpleNamespace(send_packet=AsyncMock())
    ctx.perception.self_state.gumps = MagicMock()
    ctx.perception.self_state.open_container = 0
    with (
        patch("anima.procedures.bank_deposit._find_banker", return_value=banker),
        patch(
            "anima.procedures.bank_deposit._wait_for_bank_box",
            new=AsyncMock(return_value=0x50000001),
        ),
        patch("anima.procedures.bank_deposit.asyncio.sleep", new=AsyncMock()),
    ):
        result = await proc.execute(ctx)
    assert result.success is False
    assert "nothing to deposit" in result.message
