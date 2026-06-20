"""Regression: the BankDeposit *skill* (anima.skills.trade.banking) must use
ONE weight ratio for both the trip decision (can_execute) and the heavy-material
deposit (execute).

The procedures path was already fixed (HEAVY_DEPOSIT_RATIO / _should_deposit_heavy),
but the skill path kept the split: can_execute admitted the skill at ratio > 0.6
while execute only dumped heavy DEPOSIT_GRAPHICS materials above 0.8. An
overweight miner carrying iron ore at ratio 0.6–0.8 with no gold therefore
walked to the bank, opened the box, deposited NOTHING, and returned
success=False with a -0.5 reward — a wasted round-trip that never lowered the
weight that triggered it, poisoning the reward/skill signal.

These tests pin the skill to the single shared HEAVY_DEPOSIT_RATIO constant.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.skills.trade.banking import (
    HEAVY_DEPOSIT_RATIO,
    BankDeposit,
)

ORE_GRAPHIC = 0x19B7  # in DEPOSIT_GRAPHICS, not a kept tool
BACKPACK = 0x40000001
BANK_SERIAL = 0x50000001


def _item(serial, graphic, *, container, amount=1):
    return SimpleNamespace(
        serial=serial, graphic=graphic, container=container, amount=amount,
        x=0, y=0, z=0,
    )


def _ctx(weight, weight_max, *, items, gold=0):
    ss = SimpleNamespace(
        x=1427, y=1683, z=0, serial=0x1, gold=gold,
        weight=weight, weight_max=weight_max,
        equipment={0x15: BACKPACK},
        gumps={},
        open_container=0,
    )
    world = SimpleNamespace(items={it.serial: it for it in items})
    ctx = MagicMock()
    ctx.perception = SimpleNamespace(self_state=ss, world=world)
    ctx.blackboard = {}
    ctx.conn = SimpleNamespace(send_packet=AsyncMock())
    return ctx


@pytest.mark.asyncio
async def test_dead_band_ratio_admits_the_skill():
    """ratio 0.65 (the old 0.6<r<0.8 dead band) with heavy ore + no gold must
    still pass can_execute — that is what makes the trip happen."""
    ore = _item(0x100, ORE_GRAPHIC, container=BACKPACK, amount=50)
    ctx = _ctx(65, 100, items=[ore], gold=0)
    banker = SimpleNamespace(serial=0x9, name="banker", body=0x190,
                             x=1427, y=1683, z=0, notoriety=None)
    skill = BankDeposit()
    with patch("anima.skills.trade.banking._find_banker", return_value=banker):
        assert await skill.can_execute(ctx) is True


@pytest.mark.asyncio
async def test_dead_band_ratio_actually_deposits_heavy_material():
    """The core fix: at ratio 0.65 with no gold, execute must dump the heavy
    ore (a real drop packet, success=True), not no-op with -0.5.

    Before the fix the heavy-deposit gate was 0.8, so this band opened the bank
    box and deposited nothing.
    """
    ore = _item(0x100, ORE_GRAPHIC, container=BACKPACK, amount=50)
    ctx = _ctx(65, 100, items=[ore], gold=0)
    banker = SimpleNamespace(serial=0x9, name="banker", body=0x190,
                             x=1427, y=1683, z=0, notoriety=None)

    drops: list = []

    def _capture_drop(serial, **kwargs):
        drops.append((serial, kwargs))
        return b""

    with (
        patch("anima.skills.trade.banking._find_banker", return_value=banker),
        patch(
            "anima.skills.trade.banking._wait_for_bank_box",
            new=AsyncMock(return_value=BANK_SERIAL),
        ),
        patch("anima.skills.trade.banking.asyncio.sleep", new=AsyncMock()),
        patch("anima.skills.trade.banking.build_pick_up", return_value=b""),
        patch(
            "anima.skills.trade.banking.build_drop_item",
            side_effect=_capture_drop,
        ),
        patch(
            "anima.skills.trade.banking.build_unicode_speech", return_value=b""
        ),
        patch(
            "anima.skills.trade.banking.build_double_click", return_value=b""
        ),
        patch("anima.core.publish.pub"),
    ):
        result = await BankDeposit().execute(ctx)

    # The heavy ore was actually dropped into the bank box, and the skill
    # succeeded — no more wasted bank trip.
    assert any(serial == ore.serial for serial, _ in drops), (
        "heavy ore in the 0.6-0.8 band must be deposited"
    )
    assert result.success is True
    assert result.reward > 0


@pytest.mark.asyncio
async def test_below_ratio_still_does_not_deposit_heavy():
    """Below the shared ratio with no gold, the heavy loop must NOT fire —
    the gate is genuinely tied to the constant, not always-on."""
    ore = _item(0x100, ORE_GRAPHIC, container=BACKPACK, amount=50)
    # ratio 0.50 < HEAVY_DEPOSIT_RATIO
    ctx = _ctx(50, 100, items=[ore], gold=0)
    banker = SimpleNamespace(serial=0x9, name="banker", body=0x190,
                             x=1427, y=1683, z=0, notoriety=None)

    drops: list = []

    with (
        patch("anima.skills.trade.banking._find_banker", return_value=banker),
        patch(
            "anima.skills.trade.banking._wait_for_bank_box",
            new=AsyncMock(return_value=BANK_SERIAL),
        ),
        patch("anima.skills.trade.banking.asyncio.sleep", new=AsyncMock()),
        patch("anima.skills.trade.banking.build_pick_up", return_value=b""),
        patch(
            "anima.skills.trade.banking.build_drop_item",
            side_effect=lambda serial, **kw: drops.append(serial) or b"",
        ),
        patch(
            "anima.skills.trade.banking.build_unicode_speech", return_value=b""
        ),
        patch(
            "anima.skills.trade.banking.build_double_click", return_value=b""
        ),
        patch("anima.core.publish.pub"),
    ):
        await BankDeposit().execute(ctx)

    assert ore.serial not in drops
    assert 0.5 < HEAVY_DEPOSIT_RATIO  # guard: 0.50 is genuinely below the gate
