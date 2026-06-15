"""PracticeMagery — grind Magery + Meditation (MAGIC profession loop).

Grind spell: Greater Heal on self (wire 29, circle 4, mana 11). It is
in the creation spellbook (content 0x382A8C38), is self-targetable and
beneficial, and at the pinned Magery 35 sits inside the gain window
(circle-4 window 22.9–62.9; circles 1–2 are past maxSkill → ZERO gain).
Fizzles still gain skill, so success for grinding = "cast resolved".

When mana runs low the procedure meditates instead — Meditation is also
a MAGIC-category skill, so regen time keeps scoring on the fitness
backbone.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.skills import SKILL_MEDITATION, meditate
from anima.actions.spells import (
    GREATER_HEAL_MANA,
    SPELL_GREATER_HEAL,
    cast_spell,
)
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_MAGERY = 25
SPELLBOOK_GRAPHICS = {0x0EFA}

CASTS_PER_RUN = 10
CAST_INTERVAL_S = 0.5  # cast_spell already awaits cursor + journal; 0.5s is just server recovery margin
MEDITATE_BELOW_MANA = GREATER_HEAL_MANA + 1


class PracticeMagery(Procedure):
    timeout_s = 180.0
    name = "practice_magery"
    description = "Cast Greater Heal on self to practice Magery; meditate when low on mana."

    def _has_spellbook(self, ctx: AgentContext) -> bool:
        # The creation spellbook is EQUIPPED (a wearable layer), so check
        # both worn equipment and the backpack.
        ss = ctx.perception.self_state
        world = ctx.perception.world
        for serial in ss.equipment.values():
            item = world.items.get(serial)
            if item is not None and item.graphic in SPELLBOOK_GRAPHICS:
                return True
        return bool(find_in_backpack(ctx, SPELLBOOK_GRAPHICS))

    async def can_start(self, ctx: AgentContext) -> bool:
        if not ctx.perception.self_state.is_alive:
            return False
        return self._has_spellbook(ctx)

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        mag = ss.skills.get(SKILL_MAGERY)
        med = ss.skills.get(SKILL_MEDITATION)
        before_mag = mag.value if mag else 0.0
        before_med = med.value if med else 0.0

        casts = fizzles = 0
        # g_00101 champion loop (fitness 233.8): count only RESOLVED casts, so a
        # low-mana meditation does NOT consume a cast slot — guarantees
        # CASTS_PER_RUN Magery checks/run (~+25% vs the for-loop that let
        # meditations eat slots). The on-disk version had regressed to a 6-cast
        # for-loop (~153); restored here. Guarded by tests/test_practice_magery.py.
        casts_done = 0
        while casts_done < CASTS_PER_RUN:
            if ss.mana < MEDITATE_BELOW_MANA:
                await meditate(ctx, target_pct=60.0, timeout=20.0)
                continue
            result = await cast_spell(
                ctx, SPELL_GREATER_HEAL,
                target_serial=ss.serial,
                mana_cost=GREATER_HEAL_MANA,
            )
            casts_done += 1
            if result.no_reagents:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="Out of reagents for Greater Heal",
                )
            if result.success:
                casts += 1
                fizzles += int(result.fizzled)
            await asyncio.sleep(CAST_INTERVAL_S)

        mag = ss.skills.get(SKILL_MAGERY)
        med = ss.skills.get(SKILL_MEDITATION)
        gains: dict[int, float] = {}
        mag_gain = max(0.0, (mag.value if mag else 0.0) - before_mag)
        med_gain = max(0.0, (med.value if med else 0.0) - before_med)
        if mag_gain:
            gains[SKILL_MAGERY] = mag_gain
        if med_gain:
            gains[SKILL_MEDITATION] = med_gain

        return ProcedureResult(
            success=casts > 0,
            reason=None if casts > 0 else FailureReason.MISSING_RESOURCE,
            message=(
                f"Cast {casts} ({fizzles} fizzles), "
                f"+{mag_gain:.1f} Magery +{med_gain:.1f} Meditation"
            ),
            skill_gains=gains,
            next_suggestion="practice_magery",
        )
