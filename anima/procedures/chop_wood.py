"""ChopWood procedure — use hatchet on trees to gather logs."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.target import use_on_target
from anima.skills.gathering.lumber import _find_nearby_tree  # noqa: F401 (re-export for patching)
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.gathering.lumber import (
    HATCHET_GRAPHICS,
    LOG_GRAPHICS,
    _find_nearby_tree,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# ServUO harvest range for lumberjacking is 2 tiles (HarvestSystem). The
# procedure swings from the agent's current footing without walking closer,
# so a tree the picker returns from up to SEARCH_RADIUS (8) tiles away is
# choppable only when it is already within this range.
CHOP_RANGE = 2


class ChopWood(Procedure):
    timeout_s = 600.0  # full gather tours run long — generous anti-freeze cap
    name = "chop_wood"
    description = "Use hatchet on a tree to chop wood."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, HATCHET_GRAPHICS):
            return False
        # _find_nearby_tree scans out to SEARCH_RADIUS (8) tiles, but this
        # procedure never walks to the tree — it swings from the current
        # footing. A tree beyond CHOP_RANGE draws a server "too far away"
        # reply, which execute() then parks in depleted_trees for the full
        # ~20-minute DEPLETED_COOLDOWN. Standing still and firing on every
        # 3-8-tile tree therefore blacklists every reachable tree in turn
        # and the agent chops nothing. Only start when the nearest tree is
        # genuinely in chop range (mirrors MineOre.can_start), and let the
        # planner's deadlock path walk us toward the forest otherwise.
        tree = _find_nearby_tree(ctx)
        if tree is None:
            return False
        tx, ty = tree[0], tree[1]
        ss = ctx.perception.self_state
        return max(abs(tx - ss.x), abs(ty - ss.y)) <= CHOP_RANGE

    # ServUO Lumberjacking result lines (Scripts/Skills/Lumberjacking.cs +
    # the harvest system). Any of these means the swing has RESOLVED, so the
    # per-swing wait can break immediately instead of napping a flat 3s. The
    # depletion/too-far lines matter most: a contended or freshly-stripped
    # tree emits them right away and used to stall the whole 3s deadline.
    _RESULT_SNIPPETS = (
        "logs into your backpack",      # success
        "put some logs",                # success (variant)
        "not enough wood",              # 500493 depleted
        "no wood here",                 # depleted (variant)
        "fail to produce",              # skill-check fail
        "too far away",                 # 500446 out of range
        "can't use",                    # blocked
    )

    # Result lines that mean *this tree* is spent or unreachable, so it must be
    # parked in the shared ``depleted_trees`` cooldown (the same map
    # ``_find_nearby_tree`` honours) — otherwise the next loop iteration just
    # re-selects and re-targets the identical exhausted tree forever.
    _SKIP_TREE_SNIPPETS = ("not enough wood", "no wood here", "too far away")

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)

        tools = find_in_backpack(ctx, HATCHET_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no hatchet",
            )

        tree_info = _find_nearby_tree(ctx)
        if tree_info is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no tree nearby",
            )

        tx, ty, tz, graphic = tree_info

        # Count logs before
        logs_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )

        result = await use_on_target(
            ctx, tools[0].serial,
            x=tx, y=ty, z=tz,
            graphic=graphic,
        )
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        # Event-driven cadence (mirrors MineOre): poll until the swing
        # resolves — new logs in the pack OR a lumberjacking result journal
        # line — and only burn the full window on a true timeout. The old
        # flat 3s nap was the dominant dead-time sink in low-skill chopping
        # windows: every depleted/too-far/fail swing paid the whole 3s even
        # though the server had already answered in well under a second.
        chop_start = time.time()

        def _logs_in_pack() -> int:
            return sum(
                it.amount for it in world.items.values()
                if it.container == backpack and it.graphic in LOG_GRAPHICS
            )

        def _journal_result_seen() -> str | None:
            """Return the first matching result snippet since the swing, or None."""
            for entry in ctx.perception.social.journal:
                if entry.timestamp < chop_start:
                    continue
                tl = entry.text.lower()
                for s in self._RESULT_SNIPPETS:
                    if s in tl:
                        return s
            return None

        deadline = chop_start + 3.0
        result_snippet: str | None = None
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            result_snippet = _journal_result_seen()
            if _logs_in_pack() > logs_before or result_snippet:
                break

        # A depleted / out-of-range tree must be parked in the shared cooldown
        # so _find_nearby_tree skips it next iteration; otherwise the loop pins
        # on the same dead tree and chops nothing for DEPLETED_COOLDOWN seconds.
        if result_snippet in self._SKIP_TREE_SNIPPETS:
            depleted: dict[tuple[int, int], float] = ctx.blackboard.setdefault(
                "depleted_trees", {}
            )
            depleted[(tx, ty)] = time.time()

        logs_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )
        logs_gained = logs_after - logs_before

        if logs_gained > 0:
            return ProcedureResult(
                success=True,
                message=f"Chopped {logs_gained} logs",
                next_suggestion="chop_wood",
                details={"logs": logs_gained},
            )
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Failed to chop wood",
            )
