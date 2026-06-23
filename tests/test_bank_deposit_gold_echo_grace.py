"""Regression: BankDeposit must grace-poll for a lagged gold-debit echo so the
bank-balance cache reconcile sees the REAL deposited amount, not 0.

The gold-debit echo (the 0x2E/status push that lowers ``ss.gold`` after gold is
dropped into the bank) is a separate packet from the drop ack and can lag the
flat 0.5s settle in ``execute``. If ``actual_deposited`` is read while the echo
is still in flight it is 0, so ``_reconcile_bank_after_deposit`` is handed 0 and
does nothing — the ``bank_balance`` cache keeps lagging reality by everything
just banked and the planner's buy-disable latch is never lifted, re-introducing
the gather->sell->bank->restock stall the reconcile exists to prevent.

The fix briefly polls for the echo (``ss.gold`` dropping below ``gold_before``)
before computing the deposited amount, mirroring the late-item grace-poll the
mine/smelt/chop swings already use.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.bank_deposit import BankDeposit
from anima.skills.trade.banking import GOLD_GRAPHIC


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
    # A stale, small pre-deposit reading inside the 600s freshness window — the
    # exact condition under which a missed reconcile strands the restock leg.
    ctx.blackboard = {"bank_balance": {"amount": 30, "ts": time.time()}}
    ctx.conn = SimpleNamespace(send_packet=AsyncMock())
    return ctx, ss


async def _run_execute(ctx, ss, *, echo_after: int):
    """Drive execute() with timing mocked. The gold-debit echo (ss.gold drop)
    is delivered only AFTER ``echo_after`` sleep calls — simulating the lagged
    status push that arrives during the grace-poll, not the flat 0.5s settle."""
    state = {"sleeps": 0, "gold_drop": 500}

    async def fake_sleep(_d):
        state["sleeps"] += 1
        if state["sleeps"] == echo_after:
            ss.gold = max(0, ss.gold - state["gold_drop"])

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
    return result


@pytest.mark.asyncio
async def test_lagged_gold_echo_is_grace_polled():
    """The gold debit arrives late (during the grace-poll). The reconcile must
    still see 500gp and bump the cache 30 -> 530, not be handed 0."""
    bp = 0x40000001
    gold = _item(1, GOLD_GRAPHIC, container=bp, amount=500)
    ctx, ss = _ctx([gold], gold=500)

    # Echo lands on the 6th sleep — well past the gold-loop sleeps + the 0.5s
    # settle, i.e. only the grace-poll catches it.
    result = await _run_execute(ctx, ss, echo_after=6)

    assert result.success is True
    assert result.details["gold"] == 500, (
        f"deposited amount mis-read as {result.details['gold']} — the lagged "
        "gold-debit echo was not grace-polled before the reconcile"
    )
    assert ctx.blackboard["bank_balance"]["amount"] == 530, (
        "reconcile must bump the cache by the real deposited amount"
    )


@pytest.mark.asyncio
async def test_prompt_gold_echo_unaffected():
    """When the echo lands promptly (before the settle), behaviour is unchanged:
    the deposited amount is still read correctly and the cache bumps."""
    bp = 0x40000001
    gold = _item(1, GOLD_GRAPHIC, container=bp, amount=500)
    ctx, ss = _ctx([gold], gold=500)

    # Echo on the 1st sleep — the gold debit is already visible at the settle.
    result = await _run_execute(ctx, ss, echo_after=1)

    assert result.success is True
    assert result.details["gold"] == 500
    assert ctx.blackboard["bank_balance"]["amount"] == 530
