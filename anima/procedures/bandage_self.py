"""BandageSelf — heal with bandages, grinding Healing (COMBAT category).

ServUO (Scripts/Items/Resource/Bandage.cs): double-click bandage →
target cursor → target self → "You begin applying the bandages."
(500956) → dex-scaled delay (~8s at low dex) → "You finish applying
the bandages." (500969) with a CheckSkill(Healing) roll. At full HP
the server answers "That being is not damaged!" (500955) and no gain
happens — so only run while wounded.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.journal import wait_for_journal
from anima.actions.target import use_on_object
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_HEALING = 17
BANDAGE_GRAPHICS = {0x0E21}

_FINISH_PATTERNS = [
    "finish applying",      # 500969 — heal resolved (gain rolled)
    "stanch",               # bleeding variant
    "not damaged",          # 500955 — full HP, nothing to heal
    "barely manage",        # failed heal message variants
]


class BandageSelf(Procedure):
    timeout_s = 60.0
    name = "bandage_self"
    description = "Apply a bandage to self (heals + practices Healing)."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max <= 0 or ss.hits >= ss.hits_max * 0.95:
            return False  # no damage → server refuses, no gain
        return bool(find_in_backpack(ctx, BANDAGE_GRAPHICS))

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        bandages = find_in_backpack(ctx, BANDAGE_GRAPHICS)
        if not bandages:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No bandages in backpack",
            )

        skill = ss.skills.get(SKILL_HEALING)
        before = skill.value if skill else 0.0
        hp_before = ss.hits

        since = time.time()
        used = await use_on_object(ctx, bandages[0].serial, ss.serial)
        if not used.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message=f"Bandage targeting failed: {used.message}",
            )

        # Dex-scaled apply delay — low-dex starters take ~8s
        finished = await wait_for_journal(ctx, _FINISH_PATTERNS, timeout=12.0, since=since)
        skill = ss.skills.get(SKILL_HEALING)
        gained = max(0.0, (skill.value if skill else 0.0) - before)
        healed = max(0, ss.hits - hp_before)

        if finished.success and finished.data.get("index") == 2:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="Not damaged — nothing to heal",
            )
        return ProcedureResult(
            success=True,
            message=f"Bandage applied: +{healed} HP, +{gained:.1f} Healing",
            skill_gains={SKILL_HEALING: gained} if gained else {},
        )
