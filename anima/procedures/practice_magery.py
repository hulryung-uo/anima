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
from anima.core.spells import REAGENT_GRAPHICS, get_spell_by_name, missing_reagents
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_MAGERY = 25
SPELLBOOK_GRAPHICS = {0x0EFA}

# Greater Heal is the grind spell; its reagents gate the whole run.
GREATER_HEAL_REAGENT_GRAPHICS = frozenset(REAGENT_GRAPHICS.values())

CASTS_PER_RUN = 10
CAST_INTERVAL_S = 0.5  # cast_spell already awaits cursor + journal; 0.5s is just server recovery margin
# Cast as soon as mana actually covers the spell cost. The old gate was
# GREATER_HEAL_MANA + 1, which stranded the *exact-cost* mana state: a low-Int
# starter (e.g. mana_max=18) meditates to target_pct=60% → ~10-11 mana, which
# is enough to cast Greater Heal (cost 11) but sits one short of the inflated
# +1 floor (12). The loop then re-meditates, never clears 12, trips the stall
# guard after MAX_STALLED_MEDITATIONS, and resolves ZERO casts for the whole
# run — the worst possible outcome for the MAGIC loop. Gate on the real spell
# cost so any pool that can regen to the cast price keeps casting.
MEDITATE_BELOW_MANA = GREATER_HEAL_MANA


class PracticeMagery(Procedure):
    timeout_s = 180.0
    name = "practice_magery"
    description = "Cast Greater Heal on self to practice Magery; meditate when low on mana."

    def _reagent_shortage(self, ctx: AgentContext) -> list[str]:
        """Reagent names the backpack lacks for one Greater Heal cast.

        Counts every reagent stack in the backpack (keyed by graphic id) and
        diffs it against the spell's per-cast requirement, so we fail fast with
        MISSING_RESOURCE instead of burning a cast attempt only to read the
        server's "More reagents are needed" journal line reactively.
        """
        spell = get_spell_by_name("greater heal")
        if spell is None:
            return []
        have: dict[int, int] = {}
        for item in find_in_backpack(ctx, set(GREATER_HEAL_REAGENT_GRAPHICS)):
            have[item.graphic] = have.get(item.graphic, 0) + item.amount
        return missing_reagents(spell, have)

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

    def _no_gain_possible(self, ctx: AgentContext) -> bool:
        """True when neither skill this loop rolls can gain any more.

        practice_magery rolls Magery on every cast and Meditation while
        regenerating mana. If BOTH are already at (or above) their server-
        reported cap, the whole run produces ZERO skill gain — it just burns
        the eval window casting Greater Heal forever while the profession
        loop ("first startable wins") never falls through to anything else.
        Skip so the planner can pick the next procedure instead.

        Gain is measured off ``base`` (not value+bonuses), so the cap test
        compares ``base`` against ``cap``. ``cap == 0`` means the server has
        not reported a cap yet (default), so we never skip on unknown caps —
        a missing cap must not masquerade as "capped".
        """
        ss = ctx.perception.self_state

        def _capped(skill_id: int) -> bool:
            s = ss.skills.get(skill_id)
            if s is None:
                return False
            cap = getattr(s, "cap", 0.0) or 0.0
            base = getattr(s, "base", 0.0) or 0.0
            return cap > 0.0 and base >= cap

        return _capped(SKILL_MAGERY) and _capped(SKILL_MEDITATION)

    async def can_start(self, ctx: AgentContext) -> bool:
        if not ctx.perception.self_state.is_alive:
            return False
        if not self._has_spellbook(ctx):
            return False
        if self._no_gain_possible(ctx):
            return False
        return not self._reagent_shortage(ctx)

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
        # Deadlock guard: a small mana pool can sit permanently below the cast
        # threshold (e.g. mana_max=15 → 60% target = 9 < MEDITATE_BELOW_MANA
        # = GREATER_HEAL_MANA = 11), so meditation never lifts mana enough to
        # cast. Without a bound the loop would meditate until the 180s
        # procedure timeout and resolve ZERO casts. Stop after a couple of
        # fruitless meditations so the run still returns whatever it managed
        # (and surfaces the stall).
        stalled_meditations = 0
        MAX_STALLED_MEDITATIONS = 2
        while casts_done < CASTS_PER_RUN:
            if ss.mana < MEDITATE_BELOW_MANA:
                await meditate(ctx, target_pct=60.0, timeout=20.0)
                if ss.mana < MEDITATE_BELOW_MANA:
                    stalled_meditations += 1
                    if stalled_meditations >= MAX_STALLED_MEDITATIONS:
                        break
                else:
                    stalled_meditations = 0
                continue
            stalled_meditations = 0
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
