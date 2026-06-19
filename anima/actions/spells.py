"""Spell-casting action primitives — CastSpell packet (0x12 type 0x56).

Wire spell ids are 1-BASED over ServUO's 0-based spell registry
(PacketHandlers.cs: spellID = N - 1). So Clumsy (registry 0) is wire 1,
Bless (registry 16) is wire 17, Greater Heal (registry 28) is wire 29.

Casting flow on ServUO: the cast animation runs for the circle's cast
delay, then the target cursor (0x6C) arrives — cursor arrival IS the
"cast completed" signal. Reagents and the skill check (fizzle) resolve
when the target is given. Skill GAIN happens on both success and
fizzle, so for grinding purposes a fizzle is not a failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from anima.actions.journal import wait_for_journal
from anima.actions.target import target_object, wait_for_target
from anima.client.packets import build_cast_spell

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Wire ids (registry + 1) for spells in the creation spellbook
# (Spellbook content 0x382A8C38, CharacterCreation.cs Magery case)
SPELL_HEAL = 5           # circle 1 — no gain past Magery 20
SPELL_BLESS = 17         # circle 3 — gains 8.6-48.6 (needs full book)
SPELL_GREATER_HEAL = 29  # circle 4 — gains 22.9-62.9, in creation book

GREATER_HEAL_MANA = 11
BLESS_MANA = 9

# Journal signals (ServUO Scripts/Spells/Base/Spell.cs clilocs
# 502632 / 502625 / 502630)
_FIZZLE = "fizzles"
_NO_MANA = "Insufficient mana"
_NO_REAGENTS = "More reagents are needed"


@dataclass
class CastResult:
    """Outcome of one cast attempt."""

    success: bool          # spell resolved (fizzle counts — skill gain happens)
    fizzled: bool = False
    no_mana: bool = False
    no_reagents: bool = False
    message: str = ""


async def cast_spell(
    ctx: AgentContext,
    spell_id: int,
    target_serial: int | None = None,
    mana_cost: int = 0,
    cast_delay: float = 2.5,
) -> CastResult:
    """Cast a spell and (optionally) target an object/mobile.

    target_serial=None targets self. Returns CastResult; `success` is
    True when the cast resolved (including fizzle — Magery gains on
    fizzle too). no_mana/no_reagents mean nothing was attempted/learned.
    """
    ss = ctx.perception.self_state
    if mana_cost and ss.mana_max > 0 and ss.mana < mana_cost:
        return CastResult(
            success=False, no_mana=True,
            message=f"Need {mana_cost} mana, have {ss.mana}",
        )

    since = time.time()
    ss.pending_target = None
    await ctx.conn.send_packet(build_cast_spell(spell_id))
    logger.debug("cast_spell", spell_id=spell_id)

    # Cursor arrival == cast animation finished. Give the circle's cast
    # delay plus slack; missing reagents/mana abort before the cursor.
    cursor = await wait_for_target(ctx, timeout=cast_delay + 2.0)
    if not cursor.success:
        # No cursor — look for the abort reason in the journal
        aborted = await wait_for_journal(
            ctx, [_NO_MANA, _NO_REAGENTS, _FIZZLE], timeout=0.5, since=since,
        )
        idx = aborted.data.get("index") if aborted.success else None
        return CastResult(
            success=False,
            no_mana=(idx == 0),
            no_reagents=(idx == 1),
            fizzled=(idx == 2),
            message=aborted.data.get("text", "No target cursor after cast"),
        )

    cursor_id = cursor.data.get("cursor_id", 0)
    # Echo the server's cursor flag (1=harmful for offensive spells,
    # 2=helpful for heal/bless). Strict servers reject a response whose
    # flag does not match the request, which would silently waste the cast.
    cursor_flag = cursor.data.get("cursor_flag", 0)
    await target_object(
        ctx, cursor_id, target_serial or ss.serial, cursor_flag=cursor_flag,
    )

    # Resolution: silence within the window = clean success.
    outcome = await wait_for_journal(
        ctx, [_FIZZLE, _NO_MANA, _NO_REAGENTS], timeout=1.5, since=since,
    )
    if not outcome.success:
        return CastResult(success=True, message="Cast resolved")
    idx = outcome.data.get("index")
    if idx == 0:
        # Fizzle: reagents lost, mana kept, SKILL GAINED — fine for grinding
        return CastResult(success=True, fizzled=True, message="The spell fizzled")
    return CastResult(
        success=False,
        no_mana=(idx == 1),
        no_reagents=(idx == 2),
        message=outcome.data.get("text", ""),
    )
