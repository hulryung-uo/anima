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

# Meditation journal strings (Scripts/Skills/Meditation.cs, clilocs
# 501851 / 501848)
MEDITATE_SUCCESS = "meditative trance"
MEDITATE_FAIL = "cannot focus"


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
    return await target_object(ctx, cursor_id, target_serial)


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
        ctx, [MEDITATE_SUCCESS, MEDITATE_FAIL], timeout=3.0, since=since,
    )
    if entered.success and entered.data.get("index") == 1:
        return ActionResult(success=False, message="Could not focus (meditation failed)")

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
