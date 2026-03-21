"""Banking skill — deposit gold and items at the bank."""

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
        # Must actually find a banker — _near_bank alone causes
        # infinite loops when banker is inside and we're outside
        if _find_banker(ctx):
            return True
        # Near bank but no banker visible: signal LLM to move closer
        if _near_bank(ss.x, ss.y):
            ctx.blackboard.setdefault("skill_problem", (
                "Near the bank but can't see the banker. "
                "Try moving closer to the bank entrance."
            ))
        return False

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        banker = _find_banker(ctx)
        if not banker:
            return SkillResult(success=False, reward=-1.0, message="No banker nearby")

        banker_name = banker.name or "banker"
        gold_before = ss.gold
        logger.info(
            "bank_found_npc",
            name=banker_name,
            serial=f"0x{banker.serial:08X}",
            body=f"0x{banker.body:04X}",
            pos=f"({banker.x},{banker.y},{banker.z})",
            notoriety=banker.notoriety.name if banker.notoriety else "?",
            dist=max(abs(banker.x - ss.x), abs(banker.y - ss.y)),
        )

        from anima.core.publish import pub
        pub(ctx, "action.bank_start", f"Banking {ss.gold}gp with {banker_name}", importance=2)

        # Try to open bank box:
        # 1. Say "bank" (standard ServUO speech handler)
        # 2. Also double-click banker (triggers context menu / bank gump)
        ss.gumps.clear()
        ss.open_container = 0
        await ctx.conn.send_packet(build_unicode_speech("bank"))
        await asyncio.sleep(0.3)
        # Double-click banker as fallback — some servers need this
        await ctx.conn.send_packet(build_double_click(banker.serial))
        logger.info("bank_requested", banker=banker_name, gold=ss.gold)

        # Wait for bank box via container (0x24+0x3C) or gump (0xB0/0xDD)
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
    """Wait for the bank box to open.

    Detection methods (server-dependent):
    1. 0x24 ContainerDisplay with new serial (not backpack)
    2. Equipment layer 0x1D populated
    3. 0x3C ContainerContent for a new container
    4. Gump opened (some servers use gump-based banking)
    """
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    deadline = time.monotonic() + BANK_OPEN_TIMEOUT

    while time.monotonic() < deadline:
        # Method 1: 0x24 opened a container that isn't the backpack
        if ss.open_container and ss.open_container != backpack:
            logger.debug("bank_detected_via_container", serial=f"0x{ss.open_container:08X}")
            return ss.open_container

        # Method 2: equipment layer 0x1D
        bank_serial = ss.equipment.get(LAYER_BANK)
        if bank_serial:
            return bank_serial

        # Method 3: gump appeared (server sends bank as gump)
        if ss.gumps:
            gump = next(iter(ss.gumps.values()))
            logger.debug("bank_detected_via_gump", gump_id=f"0x{gump.gump_id:08X}")
            # For gump-based banking, we can't drag-drop into it.
            # Close the gump and report — need different approach.
            return None

        await asyncio.sleep(POLL_INTERVAL)

    return None


HUMAN_BODIES = {0x0190, 0x0191}  # male, female


def _find_banker(ctx: BrainContext) -> MobileInfo | None:
    """Find a banker NPC within speech range (12 tiles).

    ServUO Banker.HandlesOnSpeech checks InRange(from, 12).
    Detection priority:
    1. Name contains "banker"
    2. INVULNERABLE notoriety
    3. Near known bank + human body NPC (fallback if notoriety not set)
    """
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=12)

    # Pass 1: name match (banker, minter, etc.)
    _BANKER_NAMES = {"banker", "minter"}
    for m in nearby:
        if m.serial == ss.serial:
            continue
        name_lower = (m.name or "").lower()
        if any(n in name_lower for n in _BANKER_NAMES):
            return m

    # Pass 2: INVULNERABLE notoriety
    for m in nearby:
        if m.serial == ss.serial:
            continue
        if m.notoriety == NotorietyFlag.INVULNERABLE:
            return m

    # Pass 3: near known bank → closest human NPC
    if _near_bank(ss.x, ss.y):
        npc_candidates = []
        for m in nearby:
            if m.serial == ss.serial:
                continue
            # Human body, not a player (low serial = NPC in most servers)
            if m.body in HUMAN_BODIES and m.serial < 0x10000:
                npc_candidates.append(m)
        if npc_candidates:
            # Pick closest
            npc_candidates.sort(
                key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y),
            )
            return npc_candidates[0]

    return None


# Known bank locations: (x, y, radius)
_BANK_LOCATIONS = [
    (1427, 1683, 20),  # West Britain Bank (from ServUO spawns)
]


def _near_bank(x: int, y: int) -> bool:
    """Check if coordinates are near a known bank location."""
    return any(
        abs(x - bx) < r and abs(y - by) < r
        for bx, by, r in _BANK_LOCATIONS
    )
