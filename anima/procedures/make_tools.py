"""MakeTools procedure — craft tools using tinkering gump.

Uses text-based button lookup (find_button_near_text) to find the correct
gump buttons, matching the approach used by the CraftTinker skill.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import wait_for_gump
from anima.actions.inventory import count_items, find_in_backpack
from anima.client.packets import build_gump_response
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.procedures.craft_blacksmith import _count_iron_ingots
from anima.skills.crafting.tinker import (
    TINKER_TOOLS_GRAPHICS,
    PICKAXE_GRAPHICS,
    HATCHET_GRAPHICS,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Minimum ingots needed to craft a tool
MIN_INGOTS_FOR_TOOL = 4

# Tinkering skill target for training mode
TINKERING_TRAIN_TARGET = 70.0
TINKERING_SKILL_ID = 37


def _get_tinker_skill(ctx: AgentContext) -> float:
    """Return current Tinkering skill value (0.0 if unknown)."""
    for sk in ctx.perception.self_state.skills.values():
        if sk.id == TINKERING_SKILL_ID:
            return sk.value
    return 0.0

SHOVEL_GRAPHICS = {0x0F39}

# Map craft target → (gump category text, item graphics)
_CRAFT_TARGETS: dict[str, tuple[str, set[int]]] = {
    "Tinker's Tools": ("Tools", TINKER_TOOLS_GRAPHICS),
    "Hatchet": ("Tools", HATCHET_GRAPHICS),
    "Pickaxe": ("Tools", PICKAXE_GRAPHICS),
    "Shovel": ("Tools", SHOVEL_GRAPHICS),
}


def _journal_craft_outcome(journal, since: float) -> str | None:
    """Classify the tinkering craft result from journal lines AFTER ``since``.

    Returns ``"create"`` (a "you create ..." success), ``"fail"`` (a "you fail"
    / "you don't have" failure), or ``None`` when no craft-result line has
    landed since the button was pressed.

    The ``since`` floor is load-bearing. ``make_tools`` re-suggests itself
    (``next_suggestion="make_tools"``) and the planner runs it back-to-back, so
    the journal routinely still holds the PREVIOUS attempt's result line within
    the recent window. Without a timestamp gate (the old ``recent(count=5)``
    scan), a stale "You create the item ..." from a prior *successful* craft is
    mis-read as THIS craft's success — a phantom tool that was never made — which
    also clears ``_make_tools_fails`` and skips the skill-too-low blacklist, so a
    genuinely failing low-Tinkering smith never trips the 5-strike give-up /
    buy-instead circuit breaker and loops on a craft it cannot land. Symmetrically
    a stale "you fail" aborts a craft that actually succeeded. Mirrors the
    ``entry.timestamp < start: continue`` reconciliation mine_ore / chop_wood /
    smelt_ore use so every gather/craft procedure reads only its OWN swing.
    """
    for entry in journal:
        if getattr(entry, "timestamp", 0.0) < since:
            continue
        tl = entry.text.lower()
        if "you create" in tl:
            return "create"
        if "you fail" in tl or "you don't have" in tl:
            return "fail"
    return None


class MakeTools(Procedure):
    name = "make_tools"
    description = "Craft tools (pickaxe, hatchet, tinker tools) using tinkering."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS):
            return False
        if _count_iron_ingots(ctx) < MIN_INGOTS_FOR_TOOL:
            return False

        # Tool-replacement mode: any required tool missing → craft it.
        has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS))
        has_hatchet = bool(find_in_backpack(ctx, HATCHET_GRAPHICS))
        has_tinker = len(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)) >= 2
        if not (has_pickaxe and has_hatchet and has_tinker):
            return True

        # Training mode: all tools present but Tinkering still below target.
        # Spare ingots get spent on extra pickaxes to raise the skill.
        return _get_tinker_skill(ctx) < TINKERING_TRAIN_TARGET

    def _decide_craft_target(self, ctx: AgentContext) -> str | None:
        """Decide what to craft based on what's missing and skill level.

        Priority is the *resource the planner routed us here to restock*, not
        skill training: a worn-out pickaxe/hatchet stalls the entire
        gather→smelt→craft loop (mine_ore.execute surfaces MISSING_RESOURCE and
        the planner selects make_tools precisely to replace it). Both Pickaxe
        and Hatchet are minSkill-0 recipes on this shard, so a low-skill smith
        CAN craft one (low success, but it eventually lands); the old
        ``skill < 25 → always Tinker's Tools`` short-circuit instead burned
        ingots on tinker tools forever and never produced the missing tool, so
        the gather loop never recovered. We only craft Tinker's Tools ahead of
        a missing gather tool when we don't yet hold a spare (execute() uses
        tools[0] and tinker tools wear out too — a self-replicating spare keeps
        the chain alive).
        """
        tinker_skill = _get_tinker_skill(ctx)

        # Bootstrap: keep a spare tinker tool (self-replicating, minSkill 0).
        # execute() consumes tools[0]; without a spare a single break ends the
        # whole tinkering chain.
        if len(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)) < 2:
            return "Tinker's Tools"

        # Restock the gather tool the planner is waiting on, at ANY skill.
        if not find_in_backpack(ctx, PICKAXE_GRAPHICS):
            return "Pickaxe"
        if not find_in_backpack(ctx, HATCHET_GRAPHICS):
            return "Hatchet"

        # Training mode — all tools present but Tinkering below target.
        # Craft extra pickaxes: useful (mining consumes them) and trains the
        # skill at the pickaxe difficulty tier (~25-70).
        if tinker_skill < TINKERING_TRAIN_TARGET:
            return "Pickaxe"

        return None

    async def _close_all_gumps(self, ctx: AgentContext) -> None:
        """Close all open gumps to prevent stale state."""
        ss = ctx.perception.self_state
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        # Close stale gumps from previous failed runs
        await self._close_all_gumps(ctx)

        tools = find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tinker tools",
            )

        if _count_iron_ingots(ctx) < MIN_INGOTS_FOR_TOOL:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="not enough ingots",
            )

        craft_target = self._decide_craft_target(ctx)
        if not craft_target:
            return ProcedureResult(success=True, message="All tools available")

        target_entry = _CRAFT_TARGETS.get(craft_target)
        if not target_entry:
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Unknown craft target: {craft_target}",
            )
        category_text, target_graphics = target_entry

        tool_serial = tools[0].serial
        ss = ctx.perception.self_state

        logger.info(
            "make_tools_start",
            target=craft_target,
            category=category_text,
            tool_serial=f"0x{tool_serial:08X}",
            ingots=_count_iron_ingots(ctx),
        )

        # 1. Open tinkering gump by double-clicking tinker tools
        from anima.client.packets import build_double_click
        ss.gumps.clear()
        await ctx.conn.send_packet(build_double_click(tool_serial))
        await asyncio.sleep(0.5)

        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tinkering gump did not open",
            )

        gump = result.data["gump"]
        logger.debug("make_tools_gump_opened", buttons=len(gump.buttons), texts=len(gump.text_lines))

        # 2. Click category (e.g. "Tools") using text-based button lookup
        cat_btn = gump.find_button_near_text(category_text)
        if cat_btn:
            logger.info("make_tools_category_found", category=category_text, button_id=cat_btn.button_id)
        if not cat_btn:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{category_text}' category not found in gump",
            )

        ss.gumps.clear()
        await ctx.conn.send_packet(
            build_gump_response(
                serial=gump.serial,
                gump_id=gump.gump_id,
                button_id=cat_btn.button_id,
            )
        )
        await asyncio.sleep(0.5)

        # 3. Wait for updated gump with the item list
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="tools list gump did not appear",
            )

        gump = result.data["gump"]

        # Count target items before crafting
        items_before = count_items(ctx, target_graphics)

        # 4. Click craft target using text-based button lookup
        item_btn = gump.find_button_near_text(craft_target)
        if not item_btn:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"'{craft_target}' not found in gump",
            )

        # Stamp the moment we press the craft button: only journal lines AFTER
        # this instant describe THIS craft. make_tools runs back-to-back, so an
        # earlier attempt's "you create"/"you fail" is still in the journal and
        # must not be attributed to this swing (see _journal_craft_outcome).
        craft_start = time.time()
        ss.gumps.clear()
        await ctx.conn.send_packet(
            build_gump_response(
                serial=gump.serial,
                gump_id=gump.gump_id,
                button_id=item_btn.button_id,
            )
        )

        # 5. Wait for crafting result
        await asyncio.sleep(4.0)

        # Check success by inventory change
        items_after = count_items(ctx, target_graphics)
        if items_after > items_before:
            ctx.blackboard.pop("_make_tools_fails", None)
            logger.info("make_tools_crafted", item=craft_target)
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=True,
                message=f"Crafted {craft_target}",
                next_suggestion="make_tools",
            )

        # Also check journal as fallback — but ONLY lines from THIS craft (after
        # the button press), so a stale prior-attempt result can't be misread.
        outcome = _journal_craft_outcome(
            ctx.perception.social.recent(count=5), craft_start
        )
        if outcome == "create":
            ctx.blackboard.pop("_make_tools_fails", None)
            logger.info("make_tools_crafted_journal", item=craft_target)
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=True,
                message=f"Crafted {craft_target}",
                next_suggestion="make_tools",
            )
        if outcome == "fail":
            logger.info("make_tools_craft_failed", item=craft_target)
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Craft failed for {craft_target}",
            )

        # Read gump notices for server feedback (notice text at y≈295)
        gump_notice = ""
        for g in ctx.perception.self_state.gumps.values():
            for t in g.texts:
                if t.y == 295:
                    gump_notice = g.get_text(t.text_id)
                    break
            if gump_notice:
                break

        notice_lower = gump_notice.lower()
        if "sufficient" in notice_lower or "enough" in notice_lower:
            logger.warning("make_tools_no_material", item=craft_target, notice=gump_notice)
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"Insufficient material for {craft_target} — {gump_notice}",
            )

        if "required skill" in notice_lower:
            logger.warning("make_tools_skill_too_low", item=craft_target, notice=gump_notice)
            ctx.blackboard["_make_tools_fails"] = 0
            ctx.blackboard["_make_tools_gave_up"] = True
            ctx.blackboard["_tinkering_blocked_until"] = time.time() + 300
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Skill too low for {craft_target} — {gump_notice}",
            )

        # Track consecutive failures — give up after 5 to prevent loop
        fail_count = ctx.blackboard.get("_make_tools_fails", 0) + 1
        ctx.blackboard["_make_tools_fails"] = fail_count
        await self._close_all_gumps(ctx)

        logger.warning(
            "make_tools_unclear",
            item=craft_target, fail_count=fail_count,
            notice=gump_notice or "(none)",
        )

        if fail_count >= 5:
            ctx.blackboard["_make_tools_fails"] = 0
            ctx.blackboard["_make_tools_gave_up"] = True  # planner should buy instead
            logger.warning("make_tools_gave_up", item=craft_target, fails=fail_count)
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Gave up crafting {craft_target} after {fail_count} attempts"
                        f" — last notice: {gump_notice or 'none'}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Craft result unclear for {craft_target} ({fail_count}/5)"
                    f" — notice: {gump_notice or 'none'}",
        )
