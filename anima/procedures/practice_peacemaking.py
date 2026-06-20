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


async def _play_through_lockout(ctx, instrument_serial: int,
                                lockout_end: float) -> int:
    """Fill the remaining skill-lockout window with instrument plays.

    Plain instrument double-clicks roll Musicianship server-side and are NOT
    gated by the 10s UseSkill lockout (field data: 200 plays in one 600s
    window). Idling here cost the first bard seed 4/5 of its gain rate —
    exclusive lockout-paced peacemaking scored 16/h vs 52/h for play-spam.

    ``lockout_end`` is an absolute ``time.monotonic()`` deadline captured at the
    ``use_skill`` call that STARTED the lockout — not a fresh duration measured
    from here. The Peacemaking lockout begins server-side the instant the skill
    is used, so the seconds the caller already spent waiting on the target
    cursor and the result journal line are ALREADY part of that window. Filling
    a fresh full ``SKILL_USE_COOLDOWN_S`` from this call double-counted that
    wait — up to ~3s of result-wait per resolved attempt — so each ~30s run
    over-slept ~6-9s it could have spent on more Musicianship plays / the next
    Peacemaking check. Filling only until ``lockout_end`` (a no-op once it has
    already elapsed) mirrors ``practice_hiding``'s ``lockout_end`` accounting.
    """
    from anima.client.packets import build_double_click

    plays = 0
    while time.monotonic() < lockout_end:
        await ctx.conn.send_packet(build_double_click(instrument_serial))
        plays += 1
        await asyncio.sleep(1.3)  # server play lockout is 1s + margin
    return plays


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
        plays = 0
        # g_00101-style throughput guard (mirrors practice_magery): count only
        # RESOLVED Peacemaking checks, so a cursor miss — the agent is still
        # inside the skill lockout, the cursor never lands — fills the lockout
        # with plays and RETRIES instead of burning one of the ATTEMPTS_PER_RUN
        # slots. The old fixed-range attempt loop let each
        # of the two `continue` branches consume an attempt without ever rolling
        # Peacemaking, so a run that hit two cursor misses rolled the skill once
        # instead of three times (~ -2/3 of the Peacemaking checks/run).
        resolved = 0
        # Bound total iterations so a cursor that NEVER arrives (e.g. a wedged
        # session) can't spin the loop forever — at worst we make
        # ATTEMPTS_PER_RUN real attempts plus a margin of pure-miss retries.
        cursor_misses = 0
        MAX_CURSOR_MISSES = ATTEMPTS_PER_RUN * 2
        while resolved < ATTEMPTS_PER_RUN and cursor_misses < MAX_CURSOR_MISSES:
            since = time.time()
            # Anchor the lockout to the instant we USE the skill — the seconds
            # spent waiting on the cursor / result below are already part of the
            # server's 10s window, so _play_through_lockout fills only what is
            # LEFT, never a fresh full window stacked on top of the result-wait.
            lockout_end = time.monotonic() + SKILL_USE_COOLDOWN_S + 0.5
            ss.pending_target = None
            await use_skill(ctx, SKILL_PEACEMAKING)

            cursor = await wait_for_target(ctx, timeout=3.0)
            if not cursor.success:
                # No cursor — most likely still inside the skill lockout.
                cursor_misses += 1
                await _play_through_lockout(ctx, instrument.serial, lockout_end)
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
                    cursor_misses += 1
                    await _play_through_lockout(ctx, instrument.serial, lockout_end)
                    continue

            # Creature cursor ("Whom do you wish to calm?" 1049525) —
            # answering with our own serial = area peace, no mobs needed.
            await target_object(ctx, cursor.data.get("cursor_id", 0), ss.serial)
            # The creature cursor was answered — the Peacemaking CheckSkill has
            # now fired server-side, so this counts as a resolved attempt.
            resolved += 1

            seen = await wait_for_journal(ctx, _RESULT_PATTERNS, timeout=3.0, since=since)
            calmed = bool(seen.success and seen.data.get("index") in (0, 1))
            if calmed:
                successes += 1
            logger.debug(
                "practice_peacemaking_attempt",
                attempt=resolved,
                calmed=calmed,
            )
            # Wait out the server skill lockout (10s on fail, 5s on
            # success — use the long one so no attempt is ever wasted),
            # filling it with Musicianship-rolling instrument plays.
            plays += await _play_through_lockout(
                ctx, instrument.serial, lockout_end)

        peace = ss.skills.get(SKILL_PEACEMAKING)
        music = ss.skills.get(SKILL_MUSICIANSHIP)
        peace_gain = max(0.0, (peace.value if peace else 0.0) - peace_before)
        music_gain = max(0.0, (music.value if music else 0.0) - music_before)
        gains: dict[int, float] = {}
        if peace_gain:
            gains[SKILL_PEACEMAKING] = peace_gain
        if music_gain:
            gains[SKILL_MUSICIANSHIP] = music_gain
        # A run that answered ZERO creature cursors rolled the Peacemaking
        # CheckSkill zero times — the loop bailed out via the bounded
        # ``cursor_misses >= MAX_CURSOR_MISSES`` guard (a wedged session, or a
        # persistent skill-lockout desync where the target cursor never lands).
        # Reporting that as ``success=True`` writes a phantom win to the
        # ActionLog reward signal and inflates the BARD skill-rate metric even
        # though no skill was practiced, exactly the phantom-success anti-pattern
        # already killed in bandage_self (a timed-out heal is a retryable
        # failure, ec92743), mine_ore / chop_wood (an unresolved swing is a
        # failure, not a success) and sell_to_vendor (gold-debited-but-no-item
        # is a FAILURE, 5a71d36). Surface it as a retryable BLOCKED failure with
        # NO re-suggestion so the planner re-evaluates instead of chaining
        # straight back into the same stall. (``resolved > 0`` — even one
        # answered cursor — is a real success: the skill rolled, plays earned
        # Musicianship, gains are credited.)
        if resolved == 0:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=(
                    f"Peacemaking practice resolved no attempts "
                    f"({cursor_misses} cursor misses) — skill never rolled"
                ),
                skill_gains=gains,
            )
        return ProcedureResult(
            success=True,
            message=(
                f"Peacemaking practice: {successes}/{resolved} calmed "
                f"({plays} plays), +{peace_gain:.1f} Peacemaking, "
                f"+{music_gain:.1f} Musicianship"
            ),
            skill_gains=gains,
            next_suggestion="practice_peacemaking",
        )
