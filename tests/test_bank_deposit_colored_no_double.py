"""Regression: an overweight load that includes colored (non-iron) ingots must
bank each colored ingot EXACTLY ONCE.

``BankDeposit.execute`` deposits colored ingots in a dedicated loop, then — when
overweight — runs a second "heavy materials" loop over ``world.items``.
``_is_heavy_depositable`` returns True for a colored ingot (non-iron hue), and
``world.items`` is not mutated by the pick_up/drop packets (the server echo lags
the tight per-item sleeps), so without an explicit skip the heavy loop lifts the
SAME colored ingot a second time — a wasted round-trip that can strand the stack
on the cursor and double-counts ``deposited_count``.

These tests assert each colored-ingot serial is picked up exactly once while a
genuine heavy material (ore) is still banked.
"""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.bank_deposit import BankDeposit
from anima.skills.crafting.smelt import INGOT_GRAPHICS

INGOT_GRAPHIC = next(iter(INGOT_GRAPHICS))
COLORED_HUE = 0x973  # any non-iron hue
ORE_GRAPHIC = 0x19B7  # in DEPOSIT_GRAPHICS, not an ingot


def _item(serial, graphic, *, container, hue=0, amount=1):
    return SimpleNamespace(
        serial=serial, graphic=graphic, container=container, hue=hue, amount=amount
    )


def _ctx(items, *, weight, weight_max, gold=0):
    bp = 0x40000001
    world = SimpleNamespace(items={it.serial: it for it in items})
    ss = SimpleNamespace(
        gold=gold,
        weight=weight,
        weight_max=weight_max,
        equipment={0x15: bp},
        gumps=MagicMock(),
        open_container=0,
    )
    ctx = MagicMock()
    ctx.perception = SimpleNamespace(self_state=ss, world=world)
    ctx.blackboard = {}
    ctx.conn = SimpleNamespace(send_packet=AsyncMock())
    return ctx, bp


async def _run_execute(ctx):
    """Drive execute() with the world frozen and timing mocked; return the
    Counter of pick_up calls keyed by serial."""
    picks: Counter[int] = Counter()

    def _fake_pick_up(serial, amount=1):
        picks[serial] += 1
        return b""

    banker = SimpleNamespace(serial=0x1234)
    with (
        patch("anima.procedures.bank_deposit._find_banker", return_value=banker),
        patch(
            "anima.procedures.bank_deposit._wait_for_bank_box",
            new=AsyncMock(return_value=0x50000001),
        ),
        patch("anima.procedures.bank_deposit.asyncio.sleep", new=AsyncMock()),
        patch("anima.procedures.bank_deposit.build_pick_up", side_effect=_fake_pick_up),
        patch("anima.procedures.bank_deposit.build_drop_item", return_value=b""),
        patch("anima.procedures.bank_deposit.build_unicode_speech", return_value=b""),
        patch("anima.procedures.bank_deposit.build_double_click", return_value=b""),
    ):
        result = await BankDeposit().execute(ctx)
    return result, picks


@pytest.mark.asyncio
async def test_overweight_colored_ingot_picked_up_once():
    """The bug: overweight + a colored ingot → the colored loop AND the heavy
    loop both lift it. After the fix each colored serial is picked up once."""
    bp = 0x40000001
    colored = _item(1, INGOT_GRAPHIC, container=bp, hue=COLORED_HUE, amount=20)
    # Overweight so the heavy loop runs (0.9 > HEAVY_DEPOSIT_RATIO=0.6).
    ctx, _ = _ctx([colored], weight=90, weight_max=100, gold=0)

    result, picks = await _run_execute(ctx)

    assert picks[colored.serial] == 1, (
        f"colored ingot lifted {picks[colored.serial]}x — heavy loop "
        "re-deposited an already-banked colored ingot"
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_overweight_ore_still_banked_once():
    """A genuine heavy material (ore) is still banked exactly once — the fix
    only excludes colored ingots from the heavy loop, nothing else."""
    bp = 0x40000001
    ore = _item(2, ORE_GRAPHIC, container=bp, amount=30)
    ctx, _ = _ctx([ore], weight=90, weight_max=100, gold=0)

    result, picks = await _run_execute(ctx)

    assert picks[ore.serial] == 1
    assert result.success is True


@pytest.mark.asyncio
async def test_overweight_mixed_load_each_lifted_once():
    """Colored ingot + ore together: colored once (colored loop), ore once
    (heavy loop), no double pick-up of the colored ingot."""
    bp = 0x40000001
    colored = _item(1, INGOT_GRAPHIC, container=bp, hue=COLORED_HUE, amount=15)
    ore = _item(2, ORE_GRAPHIC, container=bp, amount=30)
    ctx, _ = _ctx([colored, ore], weight=95, weight_max=100, gold=0)

    result, picks = await _run_execute(ctx)

    assert picks[colored.serial] == 1
    assert picks[ore.serial] == 1
    assert result.success is True
