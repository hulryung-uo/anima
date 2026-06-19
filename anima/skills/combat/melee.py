"""Melee attack skill — engage hostile targets in combat."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_attack, build_war_mode
from anima.actions.loot import find_corpses
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

# Scan radius used by ``_find_target`` — also the radius we search for the
# corpse that proves a vanished target was actually felled (vs. having fled).
ENGAGE_RANGE = 10

# How long to fight before giving up (seconds)
COMBAT_TIMEOUT = 30.0
COMBAT_TICK = 1.0

# How recently we must have taken damage to fight back in defensive mode
DEFENSIVE_WINDOW = 10.0


def _confirm_kill(ctx: BrainContext, last_hits: int, last_hits_max: int) -> bool:
    """True only when a target that vanished from ``world.mobiles`` actually DIED.

    A mobile is dropped from ``world.mobiles`` by a 0x1D Delete and by the
    stale-update prune — but UO sends a 0x1D Delete whenever a mobile leaves the
    player's visual range, not only when it dies, and the prune likewise fires
    on a mob that simply pathed away. So ``mob is None`` does NOT imply a kill:
    a fast kiter that runs out of view disappears exactly like a felled one.
    Crediting every disappearance as a kill hands MeleeAttack a +15 reward (and
    a ``success=True``) for a foe that escaped, polluting the Q-learning reward
    signal. Mirror ``combat_loop._confirm_kill`` and require positive evidence:

      * the target's LAST-KNOWN health bar was a real reading at/below zero
        (``last_hits_max > 0 and last_hits <= 0`` — we watched it die); or
      * a corpse (graphic 0x2006) is on the ground within ``ENGAGE_RANGE``.

    Otherwise the target merely left view: not a kill.
    """
    if last_hits_max > 0 and last_hits <= 0:
        return True
    return bool(find_corpses(ctx, max_dist=ENGAGE_RANGE))


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
        # Last health-bar reading we saw for the target, so when it vanishes from
        # world.mobiles we can tell a real kill (we watched it hit zero, or a
        # corpse is on the ground) from a kiter that simply left view.
        last_hits = 0
        last_hits_max = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(COMBAT_TICK)

            # Check if target is gone (dead/fled)
            mob = ctx.perception.world.mobiles.get(target_serial)
            if mob is None:
                # Gone from the world is NOT proof of death — a 0x1D Delete also
                # fires when a mob leaves visual range. Only credit a kill with
                # positive evidence; otherwise the target fled and we disengage.
                target_killed = _confirm_kill(ctx, last_hits, last_hits_max)
                if not target_killed:
                    logger.info("melee_target_left_view", target=target_name)
                break
            last_hits = mob.hits
            last_hits_max = mob.hits_max

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
        # Skip invulnerable / blessed mobiles (yellow health bar). ServUO marks
        # Healers, town NPCs and other protected mobiles with the YELLOW_BAR
        # flag (MobileInfo.is_yellow_health, synced from the 0x78/0x77/0x17
        # status packets) — they take zero damage however long we swing. This
        # mirrors the procedures path (combat_loop._find_target): without the
        # guard such a mob is a valid candidate, gets selected, and the engage
        # loop locks onto it (it never dies, never leaves view) — MeleeAttack
        # then burns the full COMBAT_TIMEOUT and returns success=False with a
        # -5 reward, polluting the Q-learning signal. Acutely relevant since the
        # survival-arena Healer NPC was co-located at the resurrection / fight
        # anchor (kernel commits K1+/K1/K3): the agent would farm swings on the
        # immortal healer instead of the Ettins/HeadlessOnes beside it. `is True`
        # (not truthy) so SimpleNamespace test mobs lacking the field aren't
        # mis-excluded.
        if getattr(m, "is_yellow_health", False) is True:
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
