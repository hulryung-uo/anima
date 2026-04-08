"""Deadlock resolution and forum escalation.

Extracted from planner.py in Task 4.2 to shrink that file and keep
deadlock-recovery logic in one focused module.

The DeadlockResolver class takes a Planner reference in its constructor
so it can read/write planner state (self._planner._failed_destinations,
self._planner._last_escalation, etc.) without requiring those fields to
become public.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.planner.planner import Planner

logger = structlog.get_logger()


class DeadlockResolver:
    """Runs deadlock recovery strategies and forum escalation for a Planner."""

    def __init__(self, planner: "Planner") -> None:
        self._planner = planner

    async def resolve(self, ctx: "AgentContext") -> None:
        """Try to break out of a deadlock state."""
        from anima.actions.inventory import find_in_backpack, count_items
        from anima.skills.gathering.mine import PICKAXE_GRAPHICS, ORE_GRAPHICS

        ss = ctx.perception.self_state
        has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}))
        ore = count_items(ctx, ORE_GRAPHICS)
        from anima.procedures.craft_blacksmith import _count_iron_ingots
        ingots = _count_iron_ingots(ctx)

        logger.warning(
            "planner_deadlock_analysis",
            pos=f"({ss.x},{ss.y})",
            gold=ss.gold,
            weight=f"{ss.weight}/{ss.weight_max}",
            has_pickaxe=has_pickaxe,
            ore=ore,
            ingots=ingots,
            failed_dests=len(self._planner._failed_destinations),
            idle_ticks=self._planner._idle_ticks,
        )

        ctx.blackboard["planner_intent"] = "교착 상태 분석 중..."

        # Strategy 1: Clear stale failed destinations (maybe they're available now)
        now = _time.time()
        cleared = 0
        for key in list(self._planner._failed_destinations.keys()):
            if now - self._planner._failed_destinations[key] > 120:  # 2min old → clear
                del self._planner._failed_destinations[key]
                cleared += 1
        if cleared:
            logger.info("planner_cleared_failed_destinations", count=cleared)
            ctx.blackboard["planner_intent"] = f"실패 목적지 {cleared}개 초기화 → 재시도"
            self._planner._idle_ticks = 0  # give the planner another chance
            return

        # Strategy 2: Clear depleted ore banks (maybe they've regenerated).
        # Both the CircuitBreaker and the legacy dict are cleared so they
        # stay in sync. The breaker's own auto-expiry handles the 10-20 min
        # respawn window in steady state; this is the deadlock-escape hammer.
        from anima.skills.gathering.mine import DEPLETED_COOLDOWN as _DEPL_CD
        depleted_banks = ctx.blackboard.get("depleted_banks", {})
        old_banks = [k for k, v in depleted_banks.items() if now - v > _DEPL_CD]
        for k in old_banks:
            del depleted_banks[k]
        breaker = ctx.blackboard.get("_bank_breaker")
        open_before: list = []
        if breaker is not None:
            open_before = list(breaker.open_targets())
            for target in open_before:
                breaker.reset(target)
        if old_banks or open_before:
            cleared_total = len(old_banks) + len(open_before)
            logger.info(
                "planner_cleared_depleted_banks",
                count_dict=len(old_banks),
                count_breaker=len(open_before),
                total=cleared_total,
            )
            ctx.blackboard["planner_intent"] = (
                f"고갈 광산 {cleared_total}개 초기화 → 재시도"
            )
            self._planner._idle_ticks = 0
            return

        # Strategy 3: Clear refused vendors
        refused = ctx.blackboard.get("refused_vendors", {})
        if refused:
            refused.clear()
            logger.info("planner_cleared_refused_vendors")
            ctx.blackboard["planner_intent"] = "거부된 벤더 초기화 → 재시도"
            self._planner._idle_ticks = 0
            return

        # Strategy 4: Clear skipped procedures
        skip = ctx.blackboard.get("_skip_procedures", set())
        if skip:
            skip.clear()
            ctx.blackboard.pop("_make_tools_gave_up", None)
            ctx.blackboard.pop("_craft_bs_fails", None)
            logger.info("planner_cleared_skip_procedures")
            ctx.blackboard["planner_intent"] = "스킵된 프로시저 초기화 → 재시도"
            self._planner._idle_ticks = 0
            return

        # Strategy 5: True deadlock — no tools, no materials (regardless of gold)
        # Reset failed destinations so the agent can walk to vendors or scavenge
        if not has_pickaxe and ore == 0 and ingots == 0:
            logger.warning(
                "planner_true_deadlock_recovery",
                reason="no tools, no materials — clearing state for vendor/scavenge",
                pos=f"({ss.x},{ss.y})",
                gold=ss.gold,
            )
            # Clear failed destinations so _move_to_location can find new targets
            self._planner._failed_destinations.clear()
            self._planner._move_fail_until = 0.0
            ctx.blackboard.pop("_skip_procedures", None)
            ctx.blackboard["planner_intent"] = (
                "교착 상태: 이동 제한 해제 → 마을에서 아이템 탐색"
            )
            if ctx.bus:
                ctx.bus.publish("system.deadlock", {
                    "message": "DEADLOCK: scavenging recovery — walking to town",
                    "importance": 3,
                })
            # Reset idle_ticks to give select_procedure another chance
            # with fresh state — Priority 4f will try scavenge/walk-to-town
            self._planner._idle_ticks = 0
            return

    async def escalate_to_forum(self, ctx: "AgentContext") -> None:
        """Post a help request to forum and pause the planner."""
        # Cooldown: don't spam forum (max once per 30 min)
        if _time.time() - self._planner._last_escalation < 1800:
            return

        self._planner._last_escalation = _time.time()
        ss = ctx.perception.self_state
        persona_name = ctx.persona.name if ctx.persona else "Anima"

        # Build help message
        has_pickaxe = False
        try:
            from anima.actions.inventory import find_in_backpack
            from anima.skills.gathering.mine import PICKAXE_GRAPHICS
            has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}))
        except Exception:
            pass

        situation = (
            f"position ({ss.x},{ss.y}), gold {ss.gold}, "
            f"weight {ss.weight}/{ss.weight_max}, "
            f"pickaxe: {'yes' if has_pickaxe else 'no'}"
        )

        logger.warning("planner_forum_help_request", situation=situation)
        ctx.blackboard["planner_intent"] = "Posting help request to forum..."

        # Try posting to forum — use LLM if available for in-character writing
        if ctx.forum_client:
            try:
                title = f"{persona_name} — stranded and asking for help"
                body = await self._compose_help_post(ctx, persona_name, situation, has_pickaxe)
                post_id = await ctx.forum_client.create_post(title, body, "tavern")
                if post_id:
                    logger.info("planner_forum_help_posted", post_id=post_id)
                    if ctx.bus:
                        ctx.bus.publish("social.forum_post", {
                            "message": f"Help request posted to forum: {title}",
                            "importance": 3,
                        })
            except Exception as e:
                logger.warning("planner_forum_help_failed", error=str(e))

        # Say something in-game too
        try:
            from anima.client.packets import build_unicode_speech
            await ctx.conn.send_packet(
                build_unicode_speech(f"I'm stuck and need help. No tools or gold. At ({ss.x},{ss.y})")
            )
        except Exception:
            pass

        # Pause and wait — maybe someone will help, or supervisor will intervene
        ctx.blackboard["planner_intent"] = "Waiting for help (paused 5 min)"
        if ctx.bus:
            ctx.bus.publish("system.deadlock", {
                "message": "Forum help request posted. Waiting 5 min before retry.",
                "importance": 3,
            })

        # Wait 5 minutes, checking periodically if something changed
        for _ in range(30):  # 30 × 10s = 5min
            await asyncio.sleep(10.0)
            # Check if someone gave us tools or gold
            ss = ctx.perception.self_state
            if ss.gold >= 10:
                logger.info("planner_help_received", gold=ss.gold)
                ctx.blackboard["planner_intent"] = f"Gold received ({ss.gold}gp) — resuming"
                self._planner._idle_ticks = 0
                return
            try:
                from anima.actions.inventory import find_in_backpack
                from anima.skills.gathering.mine import PICKAXE_GRAPHICS
                if find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}):
                    logger.info("planner_help_received_tool")
                    ctx.blackboard["planner_intent"] = "Pickaxe received — resuming"
                    self._planner._idle_ticks = 0
                    return
            except Exception:
                pass

        # After 5 min wait, reset and try again
        self._planner._idle_ticks = 0
        self._planner._failed_destinations.clear()
        ctx.blackboard.pop("depleted_banks", None)
        ctx.blackboard.pop("exhausted_mines", None)
        ctx.blackboard.pop("refused_vendors", None)
        ctx.blackboard.pop("_skip_procedures", None)
        ctx.blackboard.pop("_make_tools_gave_up", None)
        logger.info("planner_full_reset_after_wait")
        ctx.blackboard["planner_intent"] = "Full reset after wait — retrying"

    async def _compose_help_post(
        self, ctx: "AgentContext", persona_name: str,
        situation: str, has_pickaxe: bool,
    ) -> str:
        """Write a help-request post for the tavern forum.

        Uses the LLM if available so the message stays in character.
        Falls back to a clean English template otherwise.
        """
        ss = ctx.perception.self_state
        # Try LLM first — keeps the post in character with the rest of the journal
        llm = getattr(ctx, "llm", None)
        if llm is not None:
            try:
                prompt = f"""You are {persona_name}, a miner and blacksmith in Ultima Online.
You are stranded and need help from other players. Write a short forum post
(2-3 short paragraphs, ~120-180 words) asking for help.

Requirements:
- Write in English. First person, in character.
- Be honest about your situation but don't whine — show resolve.
- Mention specific details: what you're missing, where you are, what you'll
  do in return if someone helps.
- No headers, no lists, no markdown — just plain prose.

Your situation:
- Status: {situation}
- Pickaxe: {'yes' if has_pickaxe else 'no'}
- Position: ({ss.x}, {ss.y}) near Minoc

Write ONLY the post body, nothing else."""
                assert llm is not None
                response = await llm.chat([
                    {"role": "user", "content": prompt},
                ])
                if response and response.text and len(response.text.strip()) > 40:
                    return response.text.strip()
            except Exception as e:
                logger.warning("planner_help_llm_failed", error=str(e))

        # Fallback: plain English template
        return (
            f"Hello, I'm {persona_name}.\n\n"
            f"I've ended up in a hard spot — {situation}. "
            f"Without tools or coin I can't keep working the mines.\n\n"
            f"If anyone passing through Minoc could spare a pickaxe or "
            f"a few gold pieces, I'd be very grateful and happy to repay "
            f"the favor in ingots once I'm back on my feet.\n\n"
            f"I'll be waiting near ({ss.x}, {ss.y})."
        )

    def find_ground_valuables(self, ctx: "AgentContext", ss) -> list:
        """Find ANY valuable items on the ground near the player.

        Used for deadlock recovery — scavenges gold, ore, ingots, tools,
        and crafted items within pickup range.
        """
        from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS

        GOLD_GRAPHIC = 0x0EED
        SHOVEL_GRAPHICS = {0x0F39}

        valuable = (
            ORE_GRAPHICS
            | INGOT_GRAPHICS
            | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS
            | TINKER_TOOLS_GRAPHICS
            | TONGS_GRAPHICS
            | {GOLD_GRAPHIC}
        )

        junk = ctx.blackboard.get("_junk_ore_serials", set())
        result = []
        for it in ctx.perception.world.items.values():
            if (it.container == 0
                    and it.graphic in valuable
                    and it.serial not in junk
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 18):
                result.append(it)

        # Sort by distance (closest first) for efficient pickup
        result.sort(key=lambda it: max(abs(it.x - ss.x), abs(it.y - ss.y)))
        return result
