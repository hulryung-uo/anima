"""Banking skill — deposit gold and items at the bank."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import (
    build_drop_item,
    build_pick_up,
    build_unicode_speech,
)
from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Bank box is equipped at layer 0x1D
LAYER_BANK = 0x1D

# Gold graphic
GOLD_GRAPHIC = 0x0EED

# Minimum gold to bother banking
GOLD_THRESHOLD = 100

# Graphics we should NOT deposit (tools we need)
KEEP_GRAPHICS = {
    0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D,  # hatchets
    0x1034, 0x1035,  # saws
    0x1EB8, 0x1EBC,  # tinker tools
    0x0E85, 0x0E86,  # pickaxes
    0x0E21,  # bandages
}

# Graphics of crafted items we should deposit to free weight
DEPOSIT_GRAPHICS = {
    0x1BD7,  # boards
    0x1BDD, 0x1BE0,  # logs
    0x19B7, 0x19B8, 0x19B9, 0x19BA,  # ore
    0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1,  # ingots
}

# Timing
BANK_OPEN_TIMEOUT = 5.0
POLL_INTERVAL = 0.2


class BankDeposit(Skill):
    """Deposit gold into the bank when near a banker."""

    name = "bank_deposit"
    category = "trade"
    description = "Deposit gold at the bank to keep it safe. Requires a banker nearby."

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        if ss.gold < GOLD_THRESHOLD:
            return False
        return bool(_find_banker(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        banker = _find_banker(ctx)
        if not banker:
            return SkillResult(success=False, reward=-1.0, message="No banker nearby")

        banker_name = banker.name or "banker"
        gold_before = ss.gold

        from anima.core.publish import pub
        pub(ctx, "action.bank_start", f"Banking {ss.gold}gp with {banker_name}", importance=2)

        # Say "bank" to open bank box
        await ctx.conn.send_packet(build_unicode_speech("bank"))
        logger.info("bank_requested", banker=banker_name, gold=ss.gold)

        # Wait for bank box to open (0x24 → 0x3C sequence)
        # The bank box serial is at equipment layer 0x1D
        bank_serial = await _wait_for_bank_box(ctx)
        if not bank_serial:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Bank box didn't open from {banker_name}",
                duration_ms=elapsed,
            )

        logger.info("bank_box_opened", serial=f"0x{bank_serial:08X}")

        # Find gold in backpack and deposit it
        backpack = ss.equipment.get(0x15)
        if not backpack:
            elapsed = (time.monotonic() - start) * 1000
            return SkillResult(
                success=False, reward=-1.0, message="No backpack", duration_ms=elapsed,
            )

        deposited_count = 0
        deposited_gold = 0

        # Deposit gold stacks
        for item in list(world.items.values()):
            if item.container == backpack and item.graphic == GOLD_GRAPHIC:
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.1)
                await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                await asyncio.sleep(0.2)
                deposited_gold += item.amount
                deposited_count += 1

        # Also deposit heavy materials if we're overweight
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.8:
            for item in list(world.items.values()):
                if (item.container == backpack
                        and item.graphic in DEPOSIT_GRAPHICS
                        and item.graphic not in KEEP_GRAPHICS):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.1)
                    await ctx.conn.send_packet(build_drop_item(item.serial, container=bank_serial))
                    await asyncio.sleep(0.2)
                    deposited_count += 1

        # Wait for server to process
        await asyncio.sleep(0.5)

        elapsed = (time.monotonic() - start) * 1000
        gold_after = ss.gold
        actual_deposited = max(0, gold_before - gold_after)

        if deposited_count > 0:
            msg = f"Deposited {actual_deposited}gp at bank"
            logger.info("bank_deposit_success", gold=actual_deposited, items=deposited_count)
            pub(ctx, "action.bank_done", msg, importance=2)
            return SkillResult(
                success=True,
                reward=3.0 + actual_deposited * 0.01,
                message=msg,
                duration_ms=elapsed,
            )
        else:
            return SkillResult(
                success=False, reward=-0.5,
                message="Nothing to deposit",
                duration_ms=elapsed,
            )


async def _wait_for_bank_box(ctx: BrainContext) -> int | None:
    """Wait for the bank box to open after saying 'bank'.

    The bank box serial comes via:
    1. Equipment layer 0x1D (always present after login)
    2. 0x24 ContainerDisplay (confirms the box is now open)
    3. 0x3C ContainerContent (items inside arrive)
    """
    ss = ctx.perception.self_state
    deadline = time.monotonic() + BANK_OPEN_TIMEOUT

    while time.monotonic() < deadline:
        bank_serial = ss.equipment.get(LAYER_BANK)
        if bank_serial:
            # Check if the container was just opened (0x24 received)
            if ss.open_container == bank_serial:
                return bank_serial
            # Or check if items appeared inside it (0x3C received)
            has_content = any(
                it.container == bank_serial
                for it in ctx.perception.world.items.values()
            )
            if has_content:
                return bank_serial
        await asyncio.sleep(POLL_INTERVAL)

    # Fallback: return bank serial if we have it, even without confirmation
    return ss.equipment.get(LAYER_BANK)


def _find_banker(ctx: BrainContext) -> MobileInfo | None:
    """Find a banker NPC nearby.

    Bankers are invulnerable NPCs near known bank locations.
    We also check the name/title for 'banker'.
    """
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=8)

    candidates = []
    for m in nearby:
        if m.serial == ss.serial:
            continue
        if m.notoriety != NotorietyFlag.INVULNERABLE:
            continue
        name_lower = (m.name or "").lower()
        if "banker" in name_lower:
            candidates.append(m)

    if not candidates:
        # Fallback: any invulnerable NPC near a known bank location
        # Britain bank is around (1434, 1699)
        for m in nearby:
            if m.serial == ss.serial:
                continue
            if m.notoriety == NotorietyFlag.INVULNERABLE:
                # Check if we're near a bank
                if _near_bank(ss.x, ss.y):
                    candidates.append(m)

    if not candidates:
        return None

    candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return candidates[0]


def _near_bank(x: int, y: int) -> bool:
    """Check if coordinates are near a known bank location."""
    # Britain West Bank area
    if abs(x - 1434) < 15 and abs(y - 1699) < 15:
        return True
    return False
