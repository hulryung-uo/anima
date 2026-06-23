"""BankDeposit procedure — deposit gold and items at the bank."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_double_click,
    build_drop_item,
    build_pick_up,
    build_unicode_speech,
)
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.banking import (
    DEPOSIT_GRAPHICS,
    GOLD_GRAPHIC,
    GOLD_THRESHOLD,
    KEEP_GRAPHICS,
    _find_banker,
    _wait_for_bank_box,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


# Ingot graphics — different graphics for different stack sizes.
# Iron ingots use hue 0; everything else (DullCopper 0x973, ShadowIron 0x966,
# Copper 0x96D, Bronze 0x972, Gold 0x8A5, Agapite 0x979, Verite 0x89F,
# Valorite 0x8AB) is a colored ingot. Source: ServUO Misc/ResourceInfo.cs.
from anima.skills.crafting.smelt import INGOT_GRAPHICS
IRON_HUE = 0


# Weight ratio (carried / capacity) at which heavy DEPOSIT_GRAPHICS materials
# (ore, logs, boards, iron ingots) become worth banking. ``can_start`` uses
# this to decide a bank trip is warranted, and ``execute`` MUST use the SAME
# threshold to actually deposit those materials once at the bank.
#
# REGRESSION GUARD: these two used to disagree — ``can_start`` triggered the
# trip at 0.6 but ``execute`` only dumped heavy materials above 0.8. An agent
# sitting at 0.6–0.8 with no gold and no colored ingots (a miner carrying iron
# ore it earned no gold for) would walk all the way to the bank, open the box,
# deposit nothing, and return a "nothing to deposit" failure — a pure wasted
# round-trip that left it overweight and unable to keep gathering. One shared
# constant keeps the "go to bank" and "deposit at bank" decisions in lockstep.
HEAVY_DEPOSIT_RATIO = 0.6


def _should_deposit_heavy(ss) -> bool:
    """True when carried weight is high enough to bank heavy materials.

    Used by both ``BankDeposit.can_start`` (should we make the trip?) and
    ``BankDeposit.execute`` (should we dump heavy materials now?) so the two
    can never disagree and strand the agent on a no-op bank visit.
    """
    return ss.weight_max > 0 and ss.weight > ss.weight_max * HEAVY_DEPOSIT_RATIO


def _reconcile_bank_after_deposit(ctx: AgentContext, *, deposited: int) -> None:
    """Add freshly-deposited gold to the cached bank balance.

    Mirror image of ``buy_from_vendor._reconcile_bank_after_buy``: a buy the
    *bank* pays decrements the cache, so a deposit that moves backpack gold
    *into* the bank must increment it by the same amount. Without this the cache
    only ever shrinks, so after a gather->sell->bank leg the agent's recorded
    bank balance lags reality by everything it just deposited.

    Why that strands the loop: the restock leg (planner Priority 4c and
    buy_from_vendor._available_funds/_spendable_per_source) reads this very
    cache. If the last check_bank_balance read a small value *before* the
    deposit, the entry is still inside the 600s freshness window, so the planner
    trusts it: it sees bal_amount < tool cost and disables buys for 10 minutes
    even though the agent just banked hundreds of gold. Bumping the cache by the
    deposited amount (and refreshing its timestamp) keeps cache == reality.

    Only touches an *existing* cache entry — like the buy-side helper. With no
    prior reading we don't know the pre-deposit balance, so leaving it absent
    lets the planner's "unknown balance" branch read it from a banker instead.
    """
    if deposited <= 0:
        return
    bal_cache = ctx.blackboard.get("bank_balance")
    if not bal_cache:
        return
    bal_cache["amount"] = max(0, bal_cache.get("amount", 0)) + deposited
    bal_cache["ts"] = time.time()

    # Lift the buy-disable latch once the refilled balance can afford a tool.
    # _buy_disabled_until is a 10-min latch set by buy_from_vendor (no delivery)
    # or the planner (cached balance below tool cost). Planner Priority 4c gates
    # the restock branch on `time.time() >= _buy_disabled_until` BEFORE it reads
    # the balance cache, so bumping the cache alone isn't enough — a deposit that
    # just banked hundreds of gold still leaves the agent tool-less for the rest
    # of the latch (the exact gather->sell->bank->restock stall, one gate deeper).
    # Once the post-deposit balance clears the cheapest-tool floor, drop the latch
    # (and the blind-walk counter) so the next plan tick re-evaluates buying.
    from anima.procedures.buy_from_vendor import _MIN_PURCHASE_FUNDS

    if bal_cache["amount"] >= _MIN_PURCHASE_FUNDS:
        if ctx.blackboard.get("_buy_disabled_until", 0):
            ctx.blackboard["_buy_disabled_until"] = 0
        if ctx.blackboard.get("_buy_blind_walk_count", 0):
            ctx.blackboard["_buy_blind_walk_count"] = 0


def _is_colored_ingot(item) -> bool:
    """True if this item is an ingot with a non-iron hue."""
    return item.graphic in INGOT_GRAPHICS and item.hue != IRON_HUE


def _is_heavy_depositable(item) -> bool:
    """True if ``execute`` would actually bank this heavy material.

    REGRESSION GUARD (sibling of HEAVY_DEPOSIT_RATIO): ``can_start`` and
    ``execute`` must agree not just on the weight *threshold* but on *which
    items* count as depositable, or the agent makes a wasted round-trip.

    DEPOSIT_GRAPHICS includes the iron-ingot graphics, but ``execute`` keeps
    iron ingots (needed for basic crafting) and KEEP_GRAPHICS tools. Before
    this helper, ``can_start`` counted any DEPOSIT_GRAPHICS item — so an
    overweight miner carrying ONLY iron ingots (no gold, no colored ingots,
    no ore/logs/boards) passed ``can_start`` (iron-ingot graphic in
    DEPOSIT_GRAPHICS) but ``execute`` skipped every one of them and returned
    "nothing to deposit". The agent stayed overweight, so it tried the trip
    again, and again — a permanent no-op bank loop. Sharing one predicate for
    "is this worth depositing?" keeps the go/deposit decisions in lockstep,
    exactly as ``_should_deposit_heavy`` does for the weight threshold.
    """
    return (
        item.graphic in DEPOSIT_GRAPHICS
        and item.graphic not in KEEP_GRAPHICS
        and not (item.graphic in INGOT_GRAPHICS and item.hue == IRON_HUE)
    )


def _has_colored_ingots(ctx: AgentContext) -> bool:
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return False
    return any(
        _is_colored_ingot(it)
        for it in ctx.perception.world.items.values()
        if it.container == backpack
    )


class BankDeposit(Procedure):
    name = "bank_deposit"
    description = "Deposit gold and heavy items at the bank."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        has_gold = ss.gold >= GOLD_THRESHOLD

        # Always bank colored ingots — they can't be used for the basic
        # iron-based crafting recipes and would otherwise pile up forever.
        has_colored = _has_colored_ingots(ctx)

        has_heavy = False
        if _should_deposit_heavy(ss):
            backpack = ss.equipment.get(0x15)
            if backpack:
                has_heavy = any(
                    _is_heavy_depositable(it)
                    for it in world.items.values()
                    if it.container == backpack
                )

        if not has_gold and not has_heavy and not has_colored:
            return False

        return _find_banker(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        banker = _find_banker(ctx)
        if not banker:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no banker nearby",
            )

        gold_before = ss.gold

        # Open bank: say "bank" + double-click banker
        ss.gumps.clear()
        ss.open_container = 0
        await ctx.conn.send_packet(build_unicode_speech("bank"))
        await asyncio.sleep(0.3)
        await ctx.conn.send_packet(build_double_click(banker.serial))

        bank_serial = await _wait_for_bank_box(ctx)
        if not bank_serial:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="bank box did not open",
            )

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        deposited_count = 0
        colored_ingots_deposited = 0
        non_gold_count = 0
        gold_deposited_attempted = False

        # Deposit gold
        for item in list(world.items.values()):
            if item.container == backpack and item.graphic == GOLD_GRAPHIC:
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_count += 1
                gold_deposited_attempted = True

        # Always deposit colored (non-iron) ingots — they're useless for
        # basic crafting and would otherwise accumulate indefinitely.
        for item in list(world.items.values()):
            if item.container == backpack and _is_colored_ingot(item):
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_count += 1
                non_gold_count += 1
                colored_ingots_deposited += item.amount

        # Deposit heavy materials if overweight (skip iron ingots — needed
        # for crafting; colored ingots already handled above).
        #
        # REGRESSION GUARD: ``_is_heavy_depositable`` is True for a colored
        # (non-iron-hue) ingot — by design, so ``can_start`` promises the trip
        # for a colored-ingot-only overweight load. But the colored-ingot loop
        # ABOVE already lifted those very items, and ``world.items`` is not
        # mutated synchronously by the pick_up/drop packets (the server echo
        # lags the tight per-item sleeps), so each colored ingot is STILL in the
        # backpack as far as this loop can see. Without an explicit skip it gets
        # picked up a SECOND time — a wasted pick_up/drop round-trip that can
        # strand the stack on the cursor, plus a double ``deposited_count``.
        # Exclude colored ingots here so each is banked exactly once.
        if _should_deposit_heavy(ss):
            for item in list(world.items.values()):
                if (item.container == backpack and _is_heavy_depositable(item)
                        and not _is_colored_ingot(item)):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.1)
                    await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                    await asyncio.sleep(0.2)
                    deposited_count += 1
                    non_gold_count += 1

        if colored_ingots_deposited:
            logger.info(
                "bank_deposited_colored_ingots",
                amount=colored_ingots_deposited,
            )

        await asyncio.sleep(0.5)

        # The gold-debit echo (a 0x2E/status push that lowers ss.gold) is a
        # SEPARATE packet from the drop ack, and on a loaded link it can lag the
        # flat 0.5s settle above — exactly the late-container-update race the
        # mine/smelt/chop swings grace-poll for. If a gold stack WAS dropped into
        # the bank but ss.gold has not dropped yet, ``actual_deposited`` reads 0,
        # so ``_reconcile_bank_after_deposit`` is handed 0 and does NOTHING: the
        # bank_balance cache is left lagging reality by everything just banked and
        # the buy-disable latch is never lifted — re-introducing the precise
        # gather->sell->bank->restock stall that reconcile was written to prevent
        # (a still-fresh small pre-deposit reading keeps the planner from buying).
        # Briefly poll for the echo before computing the deposited amount so the
        # reconcile sees the real figure. Only the gold path needs this — item
        # deposits don't feed the gold cache.
        if gold_deposited_attempted and ss.gold >= gold_before:
            grace_deadline = time.time() + 1.5
            while time.time() < grace_deadline:
                await asyncio.sleep(0.1)
                if ss.gold < gold_before:
                    break

        actual_deposited = max(0, gold_before - ss.gold)

        # A run whose ONLY action was a gold drop that never moved is a phantom
        # success. ``deposited_count`` counts every pick_up/drop packet we SENT,
        # gold included — so a gold-only attempt that the server silently
        # rejected (bank box drifted out of range, the box closed, a transient
        # drop denial) still leaves ``deposited_count > 0`` while ``ss.gold`` is
        # unchanged even after the grace-poll above. The old code then returned
        # ``success=True`` with ``gold_changed=0``: a win written to the
        # ActionLog reward signal, ``_reconcile_bank_after_deposit`` handed 0 (a
        # no-op), and the planner told the gold is banked so it never retries —
        # the agent stays over the GOLD_THRESHOLD / overweight that triggered the
        # trip. Mirror sell_to_vendor's gold-sent-but-gold-did-not-change branch
        # (a retryable BLOCKED failure, not a phantom success). Only fail when
        # NOTHING non-gold was banked either: a colored-ingot / heavy-material
        # deposit legitimately moves no gold (``actual_deposited == 0``) and is a
        # real success, so it must still pass.
        if non_gold_count == 0 and gold_deposited_attempted and actual_deposited == 0:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="gold drop sent but bank gold did not change (rejected?)",
            )

        if deposited_count > 0:
            _reconcile_bank_after_deposit(ctx, deposited=actual_deposited)
            return ProcedureResult(
                success=True,
                message=f"Deposited {actual_deposited}gp, {deposited_count} items",
                gold_changed=-actual_deposited,
                details={"items": deposited_count, "gold": actual_deposited},
            )
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="nothing to deposit",
            )
