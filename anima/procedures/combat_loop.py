"""HuntNearby — a bounded melee combat loop (COMBAT profession).

Per-swing skill gains on ServUO (BaseWeapon.cs): every swing rolls
CheckSkill on the weapon skill + Tactics + Anatomy, kill or no kill —
so simply staying engaged grinds the COMBAT category.

Safety rails: only engages ATTACKABLE/hostile notoriety (humans only if
clearly hostile), retreats to the engagement anchor below the HP floor,
caps each engagement at 45s, and always drops war mode on exit.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.equip import equip_weapon_from_pack
from anima.client.packets import build_attack, build_war_mode
from anima.perception.enums import NotorietyFlag
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
    0x0F51, 0x0F52,  # dagger
    0x0F5E, 0x0F5F,  # broadsword
    0x13FF, 0x1400,  # katana
    0x13B6, 0x13B7,  # scimitar
    0x0F61, 0x0F62,  # longsword
    0x13B9, 0x13BA,  # viking sword
}

ENGAGE_RANGE = 10        # tiles to scan for targets
ENGAGEMENT_CAP_S = 45.0  # per-target time box
RETREAT_HP_PCT = 35.0    # break off and retreat below this
TICK_S = 1.0


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

        target = _find_target(ctx)
        if target is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No hostile target nearby",
            )

        kills = 0
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

                current = ctx.perception.world.mobiles.get(target.serial)
                if current is None or getattr(current, "hits", 1) <= 0:
                    kills += 1
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
                + (" (retreated low HP)" if retreated else "")
            ),
            skill_gains={SKILL_SWORDS: gained} if gained else {},
            next_suggestion="bandage_self" if retreated else "hunt_nearby",
        )
