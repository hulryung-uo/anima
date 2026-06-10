"""PracticeHiding — grind the Hiding skill (THIEF-STEALTH profession loop).

Cheapest reliable skill in the game: no items, no reagents, no target.
ServUO (Scripts/Skills/Hiding.cs): UseSkill(21) → CheckSkill roll →
"You have hidden yourself well." (501240) on success, "You can't seem
to hide here." (501241) on fail. Gain happens on both outcomes; the
10s skill-use lockout paces the loop. Re-using while hidden re-rolls.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.journal import wait_for_journal
from anima.actions.skills import SKILL_HIDING, SKILL_USE_COOLDOWN_S, use_skill
from anima.procedures.base import Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

_HIDE_PATTERNS = [
    "hidden yourself",          # 501240 success
    "can't seem to hide",       # 501241 / "right now" variants
]

ATTEMPTS_PER_RUN = 3  # ~30s per procedure run (10s lockout between uses)


class PracticeHiding(Procedure):
    timeout_s = 120.0
    name = "practice_hiding"
    description = "Practice the Hiding skill (no items needed)."

    async def can_start(self, ctx: AgentContext) -> bool:
        return ctx.perception.self_state.is_alive

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        skill = ss.skills.get(SKILL_HIDING)
        before = skill.value if skill else 0.0

        successes = 0
        for attempt in range(ATTEMPTS_PER_RUN):
            since = time.time()
            await use_skill(ctx, SKILL_HIDING)
            seen = await wait_for_journal(ctx, _HIDE_PATTERNS, timeout=3.0, since=since)
            if seen.success and seen.data.get("index") == 0:
                successes += 1
            logger.debug(
                "practice_hiding_attempt",
                attempt=attempt + 1,
                hidden=bool(seen.success and seen.data.get("index") == 0),
            )
            if attempt < ATTEMPTS_PER_RUN - 1:
                await asyncio.sleep(SKILL_USE_COOLDOWN_S + 0.5)

        skill = ss.skills.get(SKILL_HIDING)
        after = skill.value if skill else 0.0
        gained = max(0.0, after - before)
        return ProcedureResult(
            success=True,
            message=(
                f"Hiding practice: {successes}/{ATTEMPTS_PER_RUN} hidden, "
                f"+{gained:.1f} skill"
            ),
            skill_gains={SKILL_HIDING: gained} if gained else {},
            next_suggestion="practice_hiding",
        )
