"""PracticeMusic — grind Musicianship (BARD-SOCIAL profession loop).

ServUO (BaseInstrument.cs): a plain double-click on an instrument rolls
CheckSkill(Musicianship) after a 1s internal lockout. Success/fail is
sound-only — NO journal message — so progress is detected via the 0x3A
skill delta only. Instrument uses are not consumed by plain play.

The bard persona also speaks periodically while playing: the sociability
descriptor (speech_sent / actions) is what places BARD genomes in the
mid/high social bins.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.client.packets import build_double_click, build_unicode_speech
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_MUSICIANSHIP = 29

# BaseInstrument subclasses (Scripts/Items/Equipment/Instruments/)
INSTRUMENT_GRAPHICS = {
    0x0EB3,  # lute
    0x0EB2,  # lap harp
    0x0EB1,  # standing harp
    0x0E9C,  # drum
    0x0E9D, 0x0E9E,  # tambourines
    0x2805,  # flute
}

PLAYS_PER_RUN = 10
PLAY_INTERVAL_S = 1.3   # server lockout is 1s — small margin
SPEECH_EVERY = 9        # one line per ~9 plays → mid/high sociability bin

_BARD_LINES = [
    "La la la~ a song for the road!",
    "Any requests, friends?",
    "*strums a lively tune*",
    "Music soothes even the grumpiest miner!",
]


class PracticeMusic(Procedure):
    timeout_s = 120.0
    name = "practice_music"
    description = "Play an instrument to practice Musicianship."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not ctx.perception.self_state.is_alive:
            return False
        return bool(find_in_backpack(ctx, INSTRUMENT_GRAPHICS))

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        instruments = find_in_backpack(ctx, INSTRUMENT_GRAPHICS)
        instrument = instruments[0] if instruments else None
        if instrument is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No instrument in backpack",
            )

        skill = ss.skills.get(SKILL_MUSICIANSHIP)
        before = skill.value if skill else 0.0

        plays = 0
        for play in range(PLAYS_PER_RUN):
            # Stop the moment the session drops. Musicianship success/fail is
            # sound-only (NO journal line), so the only observable proof that a
            # CheckSkill actually rolled is that the play packet was sent on a
            # LIVE connection — a disconnected ``send_packet`` is a silent no-op
            # that rolls nothing. Mirrors combat_loop's
            # ``while ... and ctx.conn.connected`` and movement's step loop.
            if not ctx.conn.connected:
                break
            await ctx.conn.send_packet(build_double_click(instrument.serial))
            plays += 1
            if (play + 1) % SPEECH_EVERY == 0:
                line = random.choice(_BARD_LINES)
                await ctx.conn.send_packet(build_unicode_speech(line))
            await asyncio.sleep(PLAY_INTERVAL_S)

        skill = ss.skills.get(SKILL_MUSICIANSHIP)
        after = skill.value if skill else 0.0
        gained = max(0.0, after - before)
        # A run that issued ZERO plays rolled the Musicianship CheckSkill zero
        # times — every ``send_packet`` landed on a dead connection (the session
        # dropped before/at the first play), so nothing was practiced. Reporting
        # that as ``success=True`` writes a phantom win to the ActionLog reward
        # signal and inflates the BARD-SOCIAL skill-rate metric even though the
        # instrument never actually played — the exact phantom-success
        # anti-pattern already killed in the sibling BARD loop
        # (practice_peacemaking, f1e95e0 / a run that resolves zero checks is a
        # failure), practice_hiding (c2a919d), bandage_self (ec92743) and
        # mine_ore / chop_wood (an unresolved swing is a failure, not a success).
        # A zero-gain run that DID play is still a real success: a failed
        # Musicianship roll legitimately grants no skill, so we gate purely on
        # whether any play was actually issued, never on ``gained``. Surface the
        # zero-play case as a retryable BLOCKED failure with NO re-suggestion so
        # the planner re-evaluates instead of chaining straight back into the
        # same stall.
        if plays == 0:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Music practice issued no plays (disconnected) — nothing rolled",
                skill_gains={SKILL_MUSICIANSHIP: gained} if gained else {},
            )
        return ProcedureResult(
            success=True,
            message=f"Played {plays} tunes, +{gained:.1f} Musicianship",
            skill_gains={SKILL_MUSICIANSHIP: gained} if gained else {},
            next_suggestion="practice_music",
        )
