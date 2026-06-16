"""HuntNearby — a bounded melee combat loop (COMBAT profession).

Per-swing skill gains on ServUO (BaseWeapon.cs): every swing rolls
CheckSkill on the weapon skill + Tactics + Anatomy, kill or no kill —
so simply staying engaged grinds the COMBAT category.

Safety rails: only engages ATTACKABLE/hostile notoriety (humans only if
clearly hostile), retreats to the engagement anchor below the HP floor,
caps each engagement at 45s, and always drops war mode on exit.

Throughput add-ons: equips a shield (Parrying stream), interleaves
fire-and-forget self-bandages between swings (Healing stream + uptime),
and loots adjacent corpses after each kill (gold/h).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.equip import equip_shield_from_pack, equip_weapon_from_pack
from anima.actions.inventory import find_in_backpack
from anima.actions.loot import find_corpses, loot_corpse
from anima.actions.target import use_on_object
from anima.client.packets import build_attack, build_war_mode
from anima.perception.enums import NotorietyFlag
from anima.procedures.bandage_self import BANDAGE_GRAPHICS
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_SWORDS = 40
SKILL_TACTICS = 27

# Same policy as anima/skills/combat/melee.py
ATTACKABLE_NOTORIETY = {
    NotorietyFlag.ATTACKABLE,
    NotorietyFlag.CRIMINAL,
    NotorietyFlag.ENEMY,
    NotorietyFlag.MURDERER,
}
HUMAN_BODIES = {0x0190, 0x0191}

WEAPON_GRAPHICS = {
    # swords
    0x0F51, 0x0F52,  # dagger
    0x0F5E, 0x0F5F,  # broadsword
    0x13FF, 0x1400,  # katana
    0x13B6, 0x13B7,  # scimitar
    0x0F61, 0x0F62,  # longsword
    0x13B9, 0x13BA,  # viking sword
    0x1441, 0x1440,  # cutlass
    # fencing
    0x1401,          # kryss
    0x1402, 0x1403,  # short spear
    0x1404, 0x1405,  # war fork
    # mace fighting
    0x0F5C, 0x0F5D,  # mace
    0x13B3, 0x13B4,  # club
    0x143A, 0x143B,  # maul
    0x1406, 0x1407,  # war mace
}

ENGAGE_RANGE = 10        # tiles to scan for targets
ENGAGEMENT_CAP_S = 45.0  # per-target time box
RETREAT_HP_PCT = 35.0    # break off and retreat below this
TICK_S = 1.0

BANDAGE_HP_PCT = 85.0    # interleave a self-bandage below this
BANDAGE_REAPPLY_S = 8.5  # min spacing — re-applying restarts the timer
LOOT_RANGE = 2           # container-open range for corpses
CORPSE_SPAWN_WAIT_S = 1.0  # kill → corpse item appears in world state


async def _maybe_bandage(ctx: AgentContext) -> None:
    """Fire-and-forget self-bandage between swings.

    Double-clicks a bandage (0x0E21) and answers the target cursor
    (0x6C) with the agent's own serial, then returns immediately — no
    waiting for the "You finish applying" journal line, so war mode
    stays on and attack re-sends continue while the bandage timer runs.
    """
    ss = ctx.perception.self_state
    if ss.hits_max <= 0 or ss.hp_percent >= BANDAGE_HP_PCT:
        return
    now = time.monotonic()
    if now - ctx.blackboard.get("_bandage_last_ts", 0.0) < BANDAGE_REAPPLY_S:
        return
    bandages = find_in_backpack(ctx, BANDAGE_GRAPHICS)
    if not bandages:
        return
    used = await use_on_object(ctx, bandages[0].serial, ss.serial, timeout=2.0)
    if used.success:
        ctx.blackboard["_bandage_last_ts"] = now
        logger.info("combat_bandage", hp_pct=round(ss.hp_percent, 1))
    else:
        logger.debug("combat_bandage_failed", message=used.message)


async def _loot_fresh_corpses(ctx: AgentContext) -> int:
    """Loot corpses adjacent to the agent after a kill. Returns gold lifted."""
    await asyncio.sleep(CORPSE_SPAWN_WAIT_S)  # let the corpse spawn in
    looted: set[int] = ctx.blackboard.setdefault("_looted_corpses", set())
    gold = 0
    for corpse in find_corpses(ctx, max_dist=LOOT_RANGE):
        if corpse.serial in looted:
            continue
        looted.add(corpse.serial)
        result = await loot_corpse(ctx, corpse.serial)
        gold += result.data.get("gold", 0)
        logger.info(
            "hunt_loot",
            corpse=f"0x{corpse.serial:08X}",
            items=result.data.get("items", 0),
            gold=result.data.get("gold", 0),
            message=result.message,
        )
    return gold


def _find_target(ctx: AgentContext):
    """Nearest attackable non-human mobile (humans only when hostile)."""
    ss = ctx.perception.self_state
    candidates = []
    for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=ENGAGE_RANGE):
        if m.notoriety not in ATTACKABLE_NOTORIETY:
            continue
        if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
            continue
        candidates.append(m)
    if not candidates:
        return None
    candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    return candidates[0]


class HuntNearby(Procedure):
    timeout_s = 180.0
    name = "hunt_nearby"
    description = "Fight nearby hostile creatures with an equipped melee weapon."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max > 0 and ss.hp_percent < 40:
            return False  # too wounded to start a fight
        # Weapon in hand or in pack
        from anima.actions.inventory import find_in_backpack
        has_weapon = (
            bool(ss.equipment.get(1)) or bool(ss.equipment.get(2))
            or bool(find_in_backpack(ctx, WEAPON_GRAPHICS))
        )
        return has_weapon and _find_target(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state
        anchor = (ss.x, ss.y)
        ctx.blackboard["combat_anchor"] = anchor

        sw = ss.skills.get(SKILL_SWORDS)
        tc = ss.skills.get(SKILL_TACTICS)
        before = (sw.value if sw else 0.0) + (tc.value if tc else 0.0)

        if not ss.equipment.get(1) and not ss.equipment.get(2):
            equipped = await equip_weapon_from_pack(ctx, WEAPON_GRAPHICS)
            if not equipped.success:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="No weapon to fight with",
                )

        # Parrying stream: raise a shield if the left hand is free.
        # Best-effort — a missing shield never fails the hunt.
        if not ss.equipment.get(2):
            try:
                shield = await equip_shield_from_pack(ctx)
                if shield.success and shield.data:
                    logger.info(
                        "hunt_shield_equipped",
                        serial=f"0x{shield.data.get('serial', 0):08X}",
                    )
            except Exception as exc:
                logger.debug("hunt_shield_skip", error=str(exc))

        target = _find_target(ctx)
        if target is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No hostile target nearby",
            )

        kills = 0
        gold_looted = 0
        retreated = False
        try:
            await ctx.conn.send_packet(build_war_mode(True))
            await ctx.conn.send_packet(build_attack(target.serial))
            deadline = time.monotonic() + ENGAGEMENT_CAP_S

            while time.monotonic() < deadline and ctx.conn.connected:
                await asyncio.sleep(TICK_S)

                if ss.hits_max > 0 and ss.hp_percent < RETREAT_HP_PCT:
                    retreated = True
                    break

                # Interleaved self-heal — sends bandage + self-target and
                # returns; the heal resolves while we keep swinging.
                await _maybe_bandage(ctx)

                current = ctx.perception.world.mobiles.get(target.serial)
                # Dead = removed from the world, or a KNOWN health bar at
                # zero. MobileInfo defaults hits/hits_max to 0 for mobiles
                # we never queried — treating that as dead made this loop
                # re-target every tick and never land a swing.
                target_dead = current is None or (
                    current.hits_max > 0 and current.hits <= 0
                )
                if target_dead:
                    kills += 1
                    gold_looted += await _loot_fresh_corpses(ctx)
                    target = _find_target(ctx)
                    if target is None:
                        break
                    await ctx.conn.send_packet(build_attack(target.serial))
                    deadline = time.monotonic() + ENGAGEMENT_CAP_S
                    continue

                # Close the gap if the target moved away
                dist = max(abs(current.x - ss.x), abs(current.y - ss.y))
                if dist > 1:
                    await go_to(
                        ctx, current.x, current.y,
                        interrupt_check=lambda: (
                            ss.hits_max > 0 and ss.hp_percent < RETREAT_HP_PCT
                        ),
                    )
                    await ctx.conn.send_packet(build_attack(target.serial))
        finally:
            # Never leave war mode on — it blocks vendors/meditation/etc.
            try:
                await ctx.conn.send_packet(build_war_mode(False))
            except Exception:
                pass

        if retreated:
            await go_to(ctx, *anchor, run=True)

        sw = ss.skills.get(SKILL_SWORDS)
        tc = ss.skills.get(SKILL_TACTICS)
        gained = max(0.0, (sw.value if sw else 0.0) + (tc.value if tc else 0.0) - before)

        return ProcedureResult(
            success=not retreated or kills > 0,
            reason=FailureReason.INTERRUPTED if retreated and kills == 0 else None,
            message=(
                f"Combat: {kills} kills, +{gained:.1f} weapon/tactics"
                + (f", looted {gold_looted} gold" if gold_looted else "")
                + (" (retreated low HP)" if retreated else "")
            ),
            skill_gains={SKILL_SWORDS: gained} if gained else {},
            next_suggestion="bandage_self" if retreated else "hunt_nearby",
        )


# Roam outward from the combat anchor in a rotating direction so a weaponed
# warrior with no target in ENGAGE_RANGE keeps hunting for one instead of
# falling through to the mining/smelt chain (the single biggest COMBAT-uptime
# leak — the "wander_for_combat" / "widen engage" content of elite hypotheses
# g_00091 / g_00070 that was named but never landed in the loop).
WANDER_RADIUS = 8
WANDER_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
# After this many consecutive empty roams (area swept, no hostile), give up and
# yield to productive work instead of looping until the planner health-break.
WANDER_MAX_EMPTY = 4


class WanderForCombat(Procedure):
    timeout_s = 60.0
    name = "wander_for_combat"
    description = (
        "Roam near the combat anchor to find hostiles when none are in range — "
        "keeps a warrior from idling into the mining loop between fights."
    )

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max > 0 and ss.hp_percent < 40:
            return False  # too wounded — let survival/bandage run first
        has_weapon = (
            bool(ss.equipment.get(1)) or bool(ss.equipment.get(2))
            or bool(find_in_backpack(ctx, WEAPON_GRAPHICS))
        )
        # Activate exactly when hunt_nearby cannot: armed, but no target in range.
        return has_weapon and _find_target(ctx) is None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state
        # Orbit the anchor (where mobs spawn / were last fought), not drift away.
        ax, ay = ctx.blackboard.get("combat_anchor", (ss.x, ss.y))
        idx = ctx.blackboard.get("_wander_dir_idx", 0) % len(WANDER_DIRS)
        ctx.blackboard["_wander_dir_idx"] = idx + 1
        dx, dy = WANDER_DIRS[idx]
        dest_x, dest_y = ax + dx * WANDER_RADIUS, ay + dy * WANDER_RADIUS

        found: dict[str, object] = {}

        def _spotted() -> bool:
            t = _find_target(ctx)
            if t is not None:
                found["t"] = t
                return True  # interrupt the walk — engage immediately
            return False

        await go_to(ctx, dest_x, dest_y, run=True, interrupt_check=_spotted)

        if found.get("t") is not None or _find_target(ctx) is not None:
            ctx.blackboard["_wander_empty"] = 0
            return ProcedureResult(
                success=True,
                message="Spotted a hostile while roaming → engaging",
                next_suggestion="hunt_nearby",
            )
        # No target. Keep roaming for a few rounds (a momentary gap between
        # fights / a target just out of range), but bound it: once we've swept
        # the area in several directions and found nothing, this spot is cleared
        # of hostiles — YIELD so the agent does productive work (mining/etc.)
        # instead of roaming until the planner's 60s health-break fires. Open
        # world keeps re-engaging (mobs respawn/roam); a depleted arena doesn't.
        empty = ctx.blackboard.get("_wander_empty", 0) + 1
        ctx.blackboard["_wander_empty"] = empty
        if empty >= WANDER_MAX_EMPTY:
            ctx.blackboard["_wander_empty"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"No hostiles after {empty} roams → yielding to other work",
            )
        return ProcedureResult(
            success=True,
            message=f"Roamed toward ({dest_x},{dest_y}); no hostile yet ({empty}/{WANDER_MAX_EMPTY})",
            next_suggestion="wander_for_combat",
        )
