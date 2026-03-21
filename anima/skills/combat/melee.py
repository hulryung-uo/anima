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

# Human body types (should not be attacked unless criminal/enemy/murderer)
HUMAN_BODIES = {0x0190, 0x0191}  # male, female

# How long to fight before giving up (seconds)
COMBAT_TIMEOUT = 30.0
COMBAT_TICK = 1.0

# How recently we must have taken damage to fight back in defensive mode
DEFENSIVE_WINDOW = 10.0


class MeleeAttack(Skill):
    """Attack a nearby hostile target with equipped weapon."""

    name = "melee_attack"
    category = "combat"
    description = "Attack the nearest hostile creature or player in melee range."

    async def can_execute(self, ctx: BrainContext) -> bool:
        if ctx.perception.self_state.hp_percent < 20:
            return False  # Too low to fight

        persona = ctx.blackboard.get("persona")
        disposition = getattr(persona, "combat_disposition", "defensive")

        if disposition == "pacifist":
            return False  # Never initiates combat

        if disposition == "defensive":
            # Only fight back if recently took damage
            elapsed = time.monotonic() - ctx.perception.self_state.last_damage_taken_at
            if elapsed > DEFENSIVE_WINDOW:
                return False

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
        from anima.data import body_name
        target_name = target.name or body_name(target.body)

        # Enter war mode
        await ctx.conn.send_packet(build_war_mode(True))
        await asyncio.sleep(0.3)

        # Attack target
        await ctx.conn.send_packet(build_attack(target_serial))
        logger.info("melee_attack_start", target=target_name)
        feed = ctx.blackboard.get("activity_feed")
        if feed:
            feed.publish("combat", f"Attacking {target_name}", importance=2)

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
            if feed:
                feed.publish("combat", f"Killed {target_name}!", importance=3)
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
            if feed:
                feed.publish("combat", f"Disengaged from {target_name}", importance=2)
            return SkillResult(
                success=False,
                reward=reward,
                message=f"Disengaged from {target_name}",
                duration_ms=elapsed,
            )


def _find_target(ctx: BrainContext):
    """Find the nearest attackable mobile.

    Human body types (players/NPCs) are only targeted if they are
    CRIMINAL, ENEMY, or MURDERER — never for mere ATTACKABLE notoriety.
    """
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=10)

    candidates = []
    for m in nearby:
        if m.notoriety not in ATTACKABLE_NOTORIETY:
            continue
        # Don't attack human bodies unless clearly hostile
        if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
            continue
        candidates.append(m)

    if not candidates:
        return None

    # Sort by distance (Manhattan)
    candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return candidates[0]
