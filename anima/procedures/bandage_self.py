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
    # 500968 "You apply the bandages, but they barely help." — the low-skill
    # heal outcome a starter grinding Healing produces most often. The old
    # pattern "barely manage" matches NO bandage cliloc (only resurrection /
    # stat-inspection strings 502396/1019033/1038xxx), so this finish line went
    # undetected: wait_for_journal ran its full 12 s timeout and the procedure
    # fell through to a phantom "+0 HP" success — roughly doubling heal cadence.
    "barely help",          # 500968 — heal rolled but barely helped
    # 500967 "You heal what little damage your patient had." — full-HP/minor
    # heal finish; without it a near-full heal also times out instead of
    # resolving immediately.
    "little damage",
]


class BandageSelf(Procedure):
    timeout_s = 60.0
    name = "bandage_self"
    description = "Apply a bandage to self (heals + practices Healing)."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max <= 0:
            return False
        # An active poison forces a cure-bandage even at full HP: ServUO's
        # Bandage.cs rolls a cure independent of the heal, so the "not damaged"
        # refusal does not apply while poisoned. Without this, the planner's
        # Priority-1c poison gate would select bandage_self only to have it
        # immediately refuse, and a high-HP poisoned agent would never cure.
        if ss.hits >= ss.hits_max * 0.95 and not getattr(ss, "is_poisoned", False):
            return False  # no damage and no poison → server refuses, no gain
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
        poisoned_before = bool(getattr(ss, "is_poisoned", False))

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
        # No finish line within the window means the bandage never RESOLVED:
        # the server was lagging, the apply was disrupted (took damage / moved),
        # or the cursor application was dropped. The old code fell straight
        # through to the success return below — re-creating the exact phantom
        # "+0 HP success" the docstring's finish-pattern fix set out to kill,
        # just reached via timeout instead of an unmatched line. A phantom
        # success writes a win to the ActionLog reward signal and tells the
        # planner the heal is done, so a genuinely-wounded agent never retries
        # the heal that never happened. Mirror mine_ore / chop_wood, which book
        # an unresolved swing as a (retryable) failure, not a success.
        if not finished.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message="Bandage did not resolve (no finish line within timeout)",
            )
        skill = ss.skills.get(SKILL_HEALING)
        gained = max(0.0, (skill.value if skill else 0.0) - before)
        healed = max(0, ss.hits - hp_before)

        # "not damaged" (500955) is a server refusal, not a heal — detect it by
        # the matched TEXT, not a positional pattern index: adding/reordering a
        # pattern (as above) must never silently turn the refusal branch into a
        # different message.
        finish_text = (finished.data.get("text") or "").lower() if finished.success else ""
        if "not damaged" in finish_text:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="Not damaged — nothing to heal",
            )
        # A poisoned cure-bandage that neither cured nor healed is a phantom
        # success. can_start lets a near-full-HP agent run bandage_self ONLY
        # while poisoned (ServUO Bandage.cs spends the resolve attempting a
        # cure, not an HP heal). But that cure rolls CheckSkill and — crucially
        # — requires Healing >= 60 AND Anatomy >= 60 (Bandage.cs ~L445); below
        # that the cure can NEVER land, yet the server still sends 500969
        # ("You finish applying the bandages.") first, so _FINISH_PATTERNS
        # matches and the bandage RESOLVES. The old code then fell straight
        # through to success=True, +0 HP on EVERY attempt for a low-skill
        # starter — the bandage was consumed, the poison still ticks, and a
        # phantom win is written to the ActionLog reward signal while the
        # planner is told the heal is done and never retries / escalates. When
        # we went in poisoned, the resolve restored no HP (healed == 0), and we
        # are STILL poisoned, the attempt accomplished nothing: book it as a
        # retryable failure exactly as the timeout branch above does, not a
        # success. (A successful cure clears ss.is_poisoned via the 0x17 status
        # push, and any HP restored — a non-poison heal that happened to fire —
        # both fall through to the real success return below.)
        still_poisoned = bool(getattr(ss, "is_poisoned", False))
        if poisoned_before and still_poisoned and healed == 0:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Cure bandage did not cure (still poisoned, no HP healed)",
                skill_gains={SKILL_HEALING: gained} if gained else {},
            )
        return ProcedureResult(
            success=True,
            message=f"Bandage applied: +{healed} HP, +{gained:.1f} Healing",
            skill_gains={SKILL_HEALING: gained} if gained else {},
        )
