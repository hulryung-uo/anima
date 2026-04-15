"""CheckBankBalance procedure — ask a banker for bank gold balance.

This server deducts vendor purchases from bank gold (not backpack), so
knowing the bank balance before attempting a buy saves doomed round
trips. The procedure walks to the nearest banker, says "balance", and
parses the cliloc response into `ctx.blackboard["bank_balance"]`:

    {"amount": int, "ts": float}

The planner's Priority 4c (no-pickaxe → buy_from_vendor) consults this
cache so it can fall through to make_tools / stop when the bank is
empty instead of burning a round-trip on a guaranteed-to-fail buy.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_unicode_speech
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.banking import _find_banker

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


_NUM_RE = re.compile(r"([\d,]+)")


def _parse_balance(text: str) -> int | None:
    """Extract a gold balance from banker cliloc speech.

    Known cliloc forms after server-side tilde substitution:
      - 1042759: "Thy current bank balance is 1,234 gold."
      - 1155855 (AccountGold): "Platinum 5 Gold 1,234"
      - 1155848 (AccountGold secure): a secondary line "...1,234"

    All three contain the balance as a comma-separated integer. We take
    the largest integer found — platinum counts are typically small and
    never outweigh gold, so max() picks the gold amount reliably.
    """
    nums: list[int] = []
    for match in _NUM_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        if not raw:
            continue
        try:
            nums.append(int(raw))
        except ValueError:
            pass
    if not nums:
        return None
    return max(nums)


def _looks_like_balance_reply(text: str) -> bool:
    """Crude filter that avoids matching unrelated speech with numbers."""
    t = text.lower()
    return (
        "bank balance" in t
        or ("platinum" in t and "gold" in t)
        or "secure account" in t
        or "thy" in t and "gold" in t
    )


class CheckBankBalance(Procedure):
    name = "check_bank_balance"
    description = "Ask a banker for the bank gold balance."

    async def can_start(self, ctx: "AgentContext") -> bool:
        return _find_banker(ctx) is not None

    async def execute(self, ctx: "AgentContext") -> ProcedureResult:
        banker = _find_banker(ctx)
        if banker is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no banker nearby",
            )

        # Subscribe BEFORE speaking so we don't miss the instant reply.
        heard: list[int] = []

        def _on_speech(_topic: str, data: dict) -> None:
            if heard:
                return  # already captured first plausible reply
            text = data.get("text") or ""
            if not _looks_like_balance_reply(text):
                return
            val = _parse_balance(text)
            if val is not None:
                heard.append(val)

        sub = None
        if ctx.bus is not None:
            sub = ctx.bus.subscribe("avatar.speech_heard", _on_speech)

        try:
            await ctx.conn.send_packet(build_unicode_speech("balance"))
            # Balance replies are usually instant; give 2 s for latency.
            for _ in range(20):
                if heard:
                    break
                await asyncio.sleep(0.1)
        finally:
            if sub is not None and ctx.bus is not None:
                try:
                    ctx.bus.unsubscribe(sub)
                except Exception:
                    pass

        if not heard:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"no balance reply from {banker.name or 'banker'}",
            )

        amount = heard[0]
        ctx.blackboard["bank_balance"] = {"amount": amount, "ts": time.time()}
        logger.info("bank_balance_read", amount=amount, banker=banker.name)
        return ProcedureResult(
            success=True,
            message=f"Bank balance: {amount:,}gp",
            details={"amount": amount},
        )
