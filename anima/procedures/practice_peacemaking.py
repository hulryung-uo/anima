"""PracticePeacemaking — grind Peacemaking + Musicianship (BARD loop).

ServUO (Scripts/Skills/Peacemaking.cs): UseSkill(9) → BaseInstrument.
PickInstrument — the first use sends an item target cursor ("What
instrument shall you play?" 500617); answering with an instrument
serial is remembered, so later uses skip straight to the creature
cursor ("Whom do you wish to calm?" 1049525). Answering THAT cursor
with our OWN serial selects area-peace mode, which needs no mobs and
rolls CheckMusicianship AND CheckSkill(Peacemaking, 0..120) — both
pinned bard skills train on every attempt.

Outcomes (all journal-visible, lockout in parens):
  500612  "You play poorly, and there is no effect."           (10s)
  500613  "You attempt to calm everyone, but fail."            (10s)
  1049648 "...there is nothing in range for you to calm."      (5s)
  500615  "You play your hypnotic music, stopping the battle." (5s)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.journal import wait_for_journal
from anima.actions.skills import SKILL_PEACEMAKING, SKILL_USE_COOLDOWN_S, use_skill
from anima.actions.target import target_object, wait_for_target
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.procedures.practice_music import INSTRUMENT_GRAPHICS, SKILL_MUSICIANSHIP

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

LUTE_GRAPHIC = 0x0EB3  # ServUO Lute.cs: base(0xEB3, ...)

_INSTRUMENT_PROMPT = "instrument shall you play"  # 500617 (first use only)
_RESULT_PATTERNS = [
    "nothing in range for you to calm",    # 1049648 success, no mobs around
    "stopping the battle",                 # 500615  success, calmed something
    "play poorly",                         # 500612  Musicianship roll failed
    "attempt to calm everyone, but fail",  # 500613  Peacemaking roll failed
]

ATTEMPTS_PER_RUN = 3  # ~30s per procedure run (10s lockout between uses)


class PracticePeacemaking(Procedure):
    timeout_s = 120.0
    name = "practice_peacemaking"
    description = "Play area peace on self to practice Peacemaking + Musicianship."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not ctx.perception.self_state.is_alive:
            return False
        return bool(find_in_backpack(ctx, INSTRUMENT_GRAPHICS))

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        instruments = find_in_backpack(ctx, INSTRUMENT_GRAPHICS)
        instrument = next(
            (it for it in instruments if it.graphic == LUTE_GRAPHIC),
            instruments[0] if instruments else None,
        )
        if instrument is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No instrument in backpack",
            )

        peace = ss.skills.get(SKILL_PEACEMAKING)
        music = ss.skills.get(SKILL_MUSICIANSHIP)
        peace_before = peace.value if peace else 0.0
        music_before = music.value if music else 0.0

        successes = 0
        for attempt in range(ATTEMPTS_PER_RUN):
            since = time.time()
            ss.pending_target = None
            await use_skill(ctx, SKILL_PEACEMAKING)

            cursor = await wait_for_target(ctx, timeout=3.0)
            if not cursor.success:
                # No cursor — most likely still inside the skill lockout.
                await asyncio.sleep(SKILL_USE_COOLDOWN_S + 0.5)
                continue

            # First-ever use asks which instrument to play (500617); the
            # prompt lands in the journal before its cursor, so a zero-wait
            # check tells us which cursor this is.
            asked = await wait_for_journal(
                ctx, [_INSTRUMENT_PROMPT], timeout=0.2, since=since,
            )
            if asked.success:
                await target_object(
                    ctx, cursor.data.get("cursor_id", 0), instrument.serial,
                )
                cursor = await wait_for_target(ctx, timeout=3.0)
                if not cursor.success:
                    await asyncio.sleep(SKILL_USE_COOLDOWN_S + 0.5)
                    continue

            # Creature cursor ("Whom do you wish to calm?" 1049525) —
            # answering with our own serial = area peace, no mobs needed.
            await target_object(ctx, cursor.data.get("cursor_id", 0), ss.serial)

            seen = await wait_for_journal(ctx, _RESULT_PATTERNS, timeout=3.0, since=since)
            calmed = bool(seen.success and seen.data.get("index") in (0, 1))
            if calmed:
                successes += 1
            logger.debug(
                "practice_peacemaking_attempt",
                attempt=attempt + 1,
                calmed=calmed,
            )
            # Wait out the server skill lockout (10s on fail, 5s on
            # success — use the long one so no attempt is ever wasted).
            await asyncio.sleep(SKILL_USE_COOLDOWN_S + 0.5)

        peace = ss.skills.get(SKILL_PEACEMAKING)
        music = ss.skills.get(SKILL_MUSICIANSHIP)
        peace_gain = max(0.0, (peace.value if peace else 0.0) - peace_before)
        music_gain = max(0.0, (music.value if music else 0.0) - music_before)
        gains: dict[int, float] = {}
        if peace_gain:
            gains[SKILL_PEACEMAKING] = peace_gain
        if music_gain:
            gains[SKILL_MUSICIANSHIP] = music_gain
        return ProcedureResult(
            success=True,
            message=(
                f"Peacemaking practice: {successes}/{ATTEMPTS_PER_RUN} calmed, "
                f"+{peace_gain:.1f} Peacemaking, +{music_gain:.1f} Musicianship"
            ),
            skill_gains=gains,
            next_suggestion="practice_peacemaking",
        )
