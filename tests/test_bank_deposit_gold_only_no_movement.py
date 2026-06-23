"""Regression: a gold-only BankDeposit whose gold never moves is a retryable
failure, not a phantom success.

``execute`` increments ``deposited_count`` for every pick_up/drop packet it
SENDS, gold included. If a gold-only deposit is silently rejected by the server
(bank box out of range, box closed, transient drop denial), ``ss.gold`` is
unchanged even after the lagged-echo grace-poll, yet ``deposited_count > 0`` —
so the old code returned ``success=True`` with ``gold_changed=0``. That phantom
win is written to the ActionLog reward signal, the reconcile is handed 0, and
the planner is told the gold is banked so it never retries while the agent
stays over the gold/weight threshold that triggered the trip. The fix surfaces
this as a BLOCKED failure (mirroring sell_to_vendor's gold-sent-no-change path)
while a colored-ingot / heavy-material-only deposit — which legitimately moves
no gold — must still succeed.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.bank_deposit import BankDeposit
from anima.procedures.base import FailureReason
from anima.skills.trade.banking import GOLD_GRAPHIC

# A colored (non-iron) ingot graphic — picked from INGOT_GRAPHICS so
# _is_colored_ingot() recognises it as bankable.
from anima.skills.crafting.smelt import INGOT_GRAPHICS

_COLORED_INGOT_GRAPHIC = sorted(INGOT_GRAPHICS)[0]


def _item(serial, graphic, *, container, hue=0, amount=1):
    return SimpleNamespace(
        serial=serial, graphic=graphic, container=container, hue=hue, amount=amount
    )


def _ctx(items, *, gold, weight=10, weight_max=100):
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
    return ctx, ss


async def _run_execute(ctx):
    """Drive execute() with all I/O mocked and the gold debit NEVER arriving."""
    async def fake_sleep(_d):
        return None

    banker = SimpleNamespace(serial=0x1234)
    with (
        patch("anima.procedures.bank_deposit._find_banker", return_value=banker),
        patch(
            "anima.procedures.bank_deposit._wait_for_bank_box",
            new=AsyncMock(return_value=0x50000001),
        ),
        patch("anima.procedures.bank_deposit.asyncio.sleep", new=fake_sleep),
        patch("anima.procedures.bank_deposit.build_pick_up", return_value=b""),
        patch("anima.procedures.bank_deposit.build_drop_item", return_value=b""),
        patch("anima.procedures.bank_deposit.build_unicode_speech", return_value=b""),
        patch("anima.procedures.bank_deposit.build_double_click", return_value=b""),
    ):
        return await BankDeposit().execute(ctx)


@pytest.mark.asyncio
async def test_gold_only_no_movement_is_failure():
    """Gold drop sent, but ss.gold never changes -> BLOCKED failure, no phantom
    success, no fabricated gold_changed."""
    bp = 0x40000001
    gold = _item(1, GOLD_GRAPHIC, container=bp, amount=500)
    ctx, ss = _ctx([gold], gold=500)

    result = await _run_execute(ctx)

    assert result.success is False, (
        "a gold-only deposit that moved no gold must be a retryable failure, "
        "not a phantom success"
    )
    assert result.reason is FailureReason.BLOCKED
    # ss.gold never moved, so nothing should be reported as deposited.
    assert (result.gold_changed or 0) == 0


@pytest.mark.asyncio
async def test_colored_ingot_only_no_gold_still_succeeds():
    """A colored-ingot-only deposit legitimately moves no gold — it must still
    succeed (the failure gate only trips on a gold-only no-movement run)."""
    bp = 0x40000001
    ingot = _item(2, _COLORED_INGOT_GRAPHIC, container=bp, hue=0x973, amount=8)
    ctx, ss = _ctx([ingot], gold=0)

    result = await _run_execute(ctx)

    assert result.success is True, (
        "a colored-ingot deposit moves no gold but is a real success"
    )
    assert result.details["items"] >= 1


@pytest.mark.asyncio
async def test_gold_actually_moved_succeeds():
    """When the gold debit DID land, the deposit succeeds as before."""
    bp = 0x40000001
    gold = _item(1, GOLD_GRAPHIC, container=bp, amount=500)
    ctx, ss = _ctx([gold], gold=500)

    # Simulate the debit echo landing immediately after the drop.
    async def fake_sleep(_d):
        ss.gold = 0

    banker = SimpleNamespace(serial=0x1234)
    with (
        patch("anima.procedures.bank_deposit._find_banker", return_value=banker),
        patch(
            "anima.procedures.bank_deposit._wait_for_bank_box",
            new=AsyncMock(return_value=0x50000001),
        ),
        patch("anima.procedures.bank_deposit.asyncio.sleep", new=fake_sleep),
        patch("anima.procedures.bank_deposit.build_pick_up", return_value=b""),
        patch("anima.procedures.bank_deposit.build_drop_item", return_value=b""),
        patch("anima.procedures.bank_deposit.build_unicode_speech", return_value=b""),
        patch("anima.procedures.bank_deposit.build_double_click", return_value=b""),
    ):
        result = await BankDeposit().execute(ctx)

    assert result.success is True
    assert result.details["gold"] == 500
