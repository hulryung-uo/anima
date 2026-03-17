"""Melee attack skill — engage hostile targets in combat."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_attack, build_war_mode
from anima.perception.enums import NotorietyFlag
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

# Notoriety values that are valid attack targets
ATTACKABLE_NOTORIETY = {
    NotorietyFlag.ATTACKABLE,
    NotorietyFlag.CRIMINAL,
    NotorietyFlag.ENEMY,
    NotorietyFlag.MURDERER,
}

# How long to fight before giving up (seconds)
COMBAT_TIMEOUT = 30.0
COMBAT_TICK = 1.0


class MeleeAttack(Skill):
    """Attack a nearby hostile target with equipped weapon."""

    name = "melee_attack"
    category = "combat"
    description = "Attack the nearest hostile creature or player in melee range."

    async def can_execute(self, ctx: BrainContext) -> bool:
        if ctx.perception.self_state.hp_percent < 20:
            return False  # Too low to fight
        return bool(_find_target(ctx))

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        start = time.monotonic()
        hp_before = ss.hits

        target = _find_target(ctx)
        if not target:
            return SkillResult(
                success=False, reward=-1.0,
                message="No hostile target nearby",
            )

        target_serial = target.serial
        target_name = target.name or f"creature (0x{target.body:04X})"

        # Enter war mode
        await ctx.conn.send_packet(build_war_mode(True))
        await asyncio.sleep(0.3)

        # Attack target
        await ctx.conn.send_packet(build_attack(target_serial))
        logger.info("melee_attack_start", target=target_name)

        # Monitor combat until target dies, we're hurt badly, or timeout
        deadline = time.monotonic() + COMBAT_TIMEOUT
        target_killed = False

        while time.monotonic() < deadline:
            await asyncio.sleep(COMBAT_TICK)

            # Check if target is gone (dead/fled)
            mob = ctx.perception.world.mobiles.get(target_serial)
            if mob is None:
                target_killed = True
                break

            # Bail if HP drops too low
            if ss.hp_percent < 15:
                logger.warning("melee_retreat", hp=ss.hits)
                break

            # Re-send attack in case it dropped
            await ctx.conn.send_packet(build_attack(target_serial))

        # Exit war mode
        await ctx.conn.send_packet(build_war_mode(False))

        elapsed = (time.monotonic() - start) * 1000
        hp_lost = max(0, hp_before - ss.hits)
        damage_penalty = hp_lost * 0.3

        if target_killed:
            reward = 15.0 - damage_penalty
            logger.info(
                "melee_kill", target=target_name,
                hp_lost=hp_lost, duration_ms=f"{elapsed:.0f}",
            )
            return SkillResult(
                success=True,
                reward=reward,
                message=f"Killed {target_name}",
                duration_ms=elapsed,
            )
        else:
            reward = -5.0 - damage_penalty
            logger.info(
                "melee_disengage", target=target_name,
                hp_lost=hp_lost, reason="timeout_or_retreat",
            )
            return SkillResult(
                success=False,
                reward=reward,
                message=f"Disengaged from {target_name}",
                duration_ms=elapsed,
            )


def _find_target(ctx: BrainContext):
    """Find the nearest attackable mobile."""
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=10)

    candidates = [
        m for m in nearby
        if m.notoriety in ATTACKABLE_NOTORIETY
    ]

    if not candidates:
        return None

    # Sort by distance (Manhattan)
    candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return candidates[0]
