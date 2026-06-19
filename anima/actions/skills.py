"""Skill-use action primitives — UseSkill packet (0x12 type 0x24) flows.

Active skills come in two wire shapes:
  - untargeted (Hiding, Meditation, …): send UseSkill, watch the journal
  - targeted (Peacemaking, Provocation, …): send UseSkill, wait for the
    target cursor (0x6C), respond with an object target

Skill ids are the ServUO SkillName enum (Server/Skills.cs) — e.g.
Hiding=21, Meditation=46, Peacemaking=9. See PERSONA_SKILLS in
anima/client/appearance.py for the full table.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.journal import wait_for_journal
from anima.actions.result import ActionResult
from anima.actions.target import target_object, wait_for_target
from anima.client.packets import build_use_skill

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_HIDING = 21
SKILL_MEDITATION = 46
SKILL_PEACEMAKING = 9
SKILL_STEALTH = 47

# ServUO Mobile skill-use lockout (seconds between active skill uses)
SKILL_USE_COOLDOWN_S = 10.0

# Meditation journal strings (Scripts/Skills/Meditation.cs OnUse).
# A trance — and the mana-regen boost — is ONLY entered on 501851. Every
# other branch leaves m.Meditating false, so mana will NOT climb: returning
# from those branches immediately (instead of polling mana for the whole
# timeout) avoids burning ~timeout seconds per call on a no-op and reporting
# a misleading success.
MEDITATE_SUCCESS = "meditative trance"
# 501850 "You cannot focus your concentration." (failed roll) and
# 501845 "You are busy ... cannot focus." (a target cursor is open) both
# contain this substring → no trance.
MEDITATE_FAIL = "cannot focus"
# 501846 "You are at peace." — mana is already at max; no trance is needed.
MEDITATE_AT_PEACE = "at peace"
# 502626 "Your hands must be free to cast spells or meditate." /
# 500135 "Regenative forces cannot penetrate your armor!" /
# 501849 "The mind is strong but the body is weak." — the meditation is
# blocked outright; no trance, no regen.
MEDITATE_BLOCKED = re.compile(
    r"hands must be free|cannot penetrate your armor|body is weak",
    re.IGNORECASE,
)


async def use_skill(ctx: AgentContext, skill_id: int) -> ActionResult:
    """Send UseSkill for an untargeted active skill. Fire-and-forget."""
    await ctx.conn.send_packet(build_use_skill(skill_id))
    logger.debug("use_skill", skill_id=skill_id)
    return ActionResult(success=True, data={"skill_id": skill_id})


async def use_skill_on(
    ctx: AgentContext,
    skill_id: int,
    target_serial: int,
    timeout: float = 3.0,
) -> ActionResult:
    """UseSkill → wait for target cursor → respond with an object target."""
    ctx.perception.self_state.pending_target = None
    await ctx.conn.send_packet(build_use_skill(skill_id))

    result = await wait_for_target(ctx, timeout=timeout)
    if not result.success:
        return result

    cursor_id = result.data.get("cursor_id", 0)
    # Echo the server's requested cursor flag (0=neutral, 1=harmful,
    # 2=helpful) back on the response. Targeted active skills send a
    # non-neutral cursor — Provocation/Discordance raise a HARMFUL (1)
    # cursor — and strict servers reject a target response whose flag does
    # not match the request, silently no-opping the skill even though we
    # returned success. ``use_on_object``/``cast_spell`` already replay it
    # (commits 775a99f / 7a60b31); this targeted-skill path dropped it,
    # defaulting to 0. ``wait_for_target`` surfaces the flag for us.
    cursor_flag = result.data.get("cursor_flag", 0)
    return await target_object(ctx, cursor_id, target_serial, cursor_flag=cursor_flag)


async def meditate(
    ctx: AgentContext,
    target_pct: float = 90.0,
    timeout: float = 30.0,
) -> ActionResult:
    """Meditate until mana reaches target_pct (or timeout/interruption).

    Sends UseSkill(Meditation), confirms the trance via the journal,
    then polls mana. Meditation gains skill on every attempt — success
    or fail — so even a failed trance is useful grinding.
    """
    ss = ctx.perception.self_state
    if ss.mana_max > 0 and (ss.mana / ss.mana_max) * 100.0 >= target_pct:
        return ActionResult(success=True, message="Mana already full")

    since = time.time()
    await ctx.conn.send_packet(build_use_skill(SKILL_MEDITATION))
    entered = await wait_for_journal(
        ctx,
        [MEDITATE_SUCCESS, MEDITATE_FAIL, MEDITATE_AT_PEACE, MEDITATE_BLOCKED],
        timeout=3.0,
        since=since,
    )
    if entered.success:
        idx = entered.data.get("index")
        if idx == 1:
            # 501850/501845: roll failed or busy — no trance was entered.
            return ActionResult(
                success=False, message="Could not focus (meditation failed)"
            )
        if idx == 2:
            # 501846 "You are at peace." — mana is already maxed. Nothing to
            # regen; report the goal as met rather than polling for a climb
            # that will never come.
            return ActionResult(
                success=True,
                message="Mana already at peace (full)",
                data={"mana": ss.mana},
            )
        if idx == 3:
            # Hands occupied / armor / low body: blocked outright, no regen.
            return ActionResult(
                success=False, message="Meditation blocked (hands/armor/body)"
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        if ss.mana_max > 0 and (ss.mana / ss.mana_max) * 100.0 >= target_pct:
            return ActionResult(
                success=True,
                message=f"Meditated to {ss.mana}/{ss.mana_max} mana",
                data={"mana": ss.mana},
            )
    # Timed out — partial regen still counts as progress
    return ActionResult(
        success=True,
        message=f"Meditation window over ({ss.mana}/{ss.mana_max} mana)",
        data={"mana": ss.mana},
    )
