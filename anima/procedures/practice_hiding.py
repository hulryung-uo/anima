"""PracticeHiding — grind Hiding + Stealth (THIEF-STEALTH profession loop).

Cheapest reliable skill in the game: no items, no reagents, no target.
ServUO (Scripts/Skills/Hiding.cs): UseSkill(21) → CheckSkill roll →
"You have hidden yourself well." (501240) on success, "You can't seem
to hide here." (501241) on fail. Gain happens on both outcomes; the
10s skill-use lockout paces the loop. Re-using while hidden re-rolls.

Stealth rides the lockout for free: while hidden, unmounted, and
Stealth ≥ 25, every walk step that exhausts AllowedStealthSteps makes
PlayerMobile.OnMove call Stealth.OnUse DIRECTLY — bypassing the global
NextSkillTime gate — which rolls CheckSkill(Stealth). Success grants
Stealth/5 more quiet steps ("You begin to move quietly." 502730);
failure reveals ("You fail in your attempt to move unnoticed." 502731).
So instead of idling through the 10s Hiding lockout after a successful
hide, we take slow single steps E/W in place and harvest Stealth rolls
until revealed or the lockout elapses, then re-attempt Hiding.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.action.movement import _walk_one_step
from anima.actions.journal import wait_for_journal
from anima.actions.skills import (
    SKILL_HIDING,
    SKILL_STEALTH,
    SKILL_USE_COOLDOWN_S,
    use_skill,
)
from anima.procedures.base import Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

_HIDE_PATTERNS = [
    "hidden yourself",          # 501240 success
    "can't seem to hide",       # 501241 / "right now" variants
]

ATTEMPTS_PER_RUN = 3  # ~30s per procedure run (10s lockout between uses)

# Stealth-walk tuning: alternate E/W single steps so the agent never
# drifts more than 1 tile from its lane workplace. 8 walk-paced steps
# (~0.45s each) + pauses fill most of the 10s Hiding lockout.
_DIR_EAST = 2
_DIR_WEST = 6
STEALTH_STEPS_MAX = 8
STEALTH_STEP_PAUSE_S = 0.7


class PracticeHiding(Procedure):
    timeout_s = 120.0
    name = "practice_hiding"
    description = "Practice Hiding (and Stealth while hidden — no items needed)."

    async def can_start(self, ctx: AgentContext) -> bool:
        return ctx.perception.self_state.is_alive

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        hiding = ss.skills.get(SKILL_HIDING)
        stealth = ss.skills.get(SKILL_STEALTH)
        hiding_before = hiding.value if hiding else 0.0
        stealth_before = stealth.value if stealth else 0.0

        successes = 0
        stealth_steps = 0
        for attempt in range(ATTEMPTS_PER_RUN):
            since = time.time()
            lockout_end = time.monotonic() + SKILL_USE_COOLDOWN_S + 0.5
            await use_skill(ctx, SKILL_HIDING)
            seen = await wait_for_journal(ctx, _HIDE_PATTERNS, timeout=3.0, since=since)
            hid = bool(seen.success and seen.data.get("index") == 0)
            if hid:
                successes += 1
                # Train Stealth through the Hiding lockout instead of idling.
                stealth_steps += await self._stealth_walk(ctx, lockout_end)
            logger.debug(
                "practice_hiding_attempt",
                attempt=attempt + 1,
                hidden=hid,
            )
            # Always wait out the 10s server skill lockout — skipping the
            # sleep after the run's LAST attempt made the first attempt of
            # every next run land inside the lockout ("You must wait a few
            # moments"), wasting 1 of every 3 Hiding rolls.
            remaining = lockout_end - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)

        hiding = ss.skills.get(SKILL_HIDING)
        stealth = ss.skills.get(SKILL_STEALTH)
        hiding_gain = max(0.0, (hiding.value if hiding else 0.0) - hiding_before)
        stealth_gain = max(0.0, (stealth.value if stealth else 0.0) - stealth_before)
        gains: dict[int, float] = {}
        if hiding_gain:
            gains[SKILL_HIDING] = hiding_gain
        if stealth_gain:
            gains[SKILL_STEALTH] = stealth_gain
        return ProcedureResult(
            success=True,
            message=(
                f"Hiding practice: {successes}/{ATTEMPTS_PER_RUN} hidden, "
                f"+{hiding_gain:.1f} Hiding; {stealth_steps} stealth steps, "
                f"+{stealth_gain:.1f} Stealth"
            ),
            skill_gains=gains,
            next_suggestion="practice_hiding",
        )

    async def _stealth_walk(self, ctx: AgentContext, lockout_end: float) -> int:
        """Take slow E/W single steps while hidden to roll Stealth checks.

        Sends UseSkill(Stealth) once for the post-lockout case (Stealth.cs
        OnUse: starts stealth mode, grants Stealth/5 quiet steps). Within
        the Hiding lockout the server rejects that packet (Skills.cs gates
        UseSkill on the global NextSkillTime), but it doesn't matter: the
        walk-triggered rolls in PlayerMobile.OnMove fire regardless.

        Stops when revealed (ss.hidden drops — server pushes self 0x78 on
        OnHiddenChanged) or when the lockout has elapsed. Returns the
        number of steps taken.
        """
        ss = ctx.perception.self_state
        await use_skill(ctx, SKILL_STEALTH)

        step_delay = ctx.cfg.movement.walk_delay_ms / 1000.0
        steps = 0
        for i in range(STEALTH_STEPS_MAX):
            if not ss.hidden:
                break  # revealed — failed Stealth roll (502731) or external
            if time.monotonic() + step_delay >= lockout_end:
                break  # lockout over — go back to Hiding rolls
            direction = _DIR_EAST if i % 2 == 0 else _DIR_WEST
            await _walk_one_step(ctx, direction, step_delay)
            steps += 1
            await asyncio.sleep(STEALTH_STEP_PAUSE_S)

        if steps:
            logger.debug("stealth_walk", steps=steps, still_hidden=ss.hidden)
        return steps
