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
        "worn out",                     # 1044038 hatchet destroyed at 0 uses
    )

    # Result lines that mean *this tree* is spent or unreachable, so it must be
    # parked in the shared ``depleted_trees`` cooldown (the same map
    # ``_find_nearby_tree`` honours) — otherwise the next loop iteration just
    # re-selects and re-targets the identical exhausted tree forever.
    _SKIP_TREE_SNIPPETS = ("not enough wood", "no wood here", "too far away")

    # 1044038 "You have worn out your tool!" — ServUO HarvestSystem deletes the
    # hatchet when UsesRemaining hits 0. This is NOT a depleted tree and NOT a
    # skill-check miss: the *tool* is gone. Mirrors mine_ore's tool_broke path.
    _TOOL_BROKE_SNIPPETS = ("worn out",)

    # Success lines specifically. As with mining, ServUO sends the "...logs
    # into your backpack" cliloc and the container-content update (0x25/0x1A)
    # as two separate packets whose relative order is NOT guaranteed; the
    # message is frequently processed first. If the wait loop breaks on the
    # success line before the log item has been applied to world.items,
    # logs_after == logs_before and a genuinely successful chop is mis-booked
    # as a FAIL with zero yield credited. Track it so we can grace-poll.
    _SUCCESS_SNIPPETS = ("logs into your backpack", "put some logs")

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

        def _skip_tree_seen() -> bool:
            """True if ANY skip-tree line (depleted / too-far) landed this swing.

            ``result_snippet`` only captures the FIRST ``_RESULT_SNIPPETS`` match
            in journal order. On the swing that *strips* a tree ServUO emits both
            a success line ("...logs into your backpack") AND a depletion line
            ("not enough wood here") within the same window, in EITHER order. If
            the success line sorts first, ``result_snippet`` is a success snippet
            (not in ``_SKIP_TREE_SNIPPETS``) and the now-empty tree was never
            parked — so the next loop iteration re-selects the same exhausted
            tree and chops nothing until depletion happens to sort first. Scan
            the whole window so parking is order-independent, mirroring
            mine_ore's terminal-flag journal reconciliation pass.
            """
            for entry in ctx.perception.social.journal:
                if entry.timestamp < chop_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in self._SKIP_TREE_SNIPPETS):
                    return True
            return False

        def _tool_broke_seen() -> bool:
            """True if a worn-out-hatchet line landed anywhere this swing."""
            for entry in ctx.perception.social.journal:
                if entry.timestamp < chop_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in self._TOOL_BROKE_SNIPPETS):
                    return True
            return False

        def _success_seen() -> bool:
            """True if a chop-success journal line landed since the swing."""
            for entry in ctx.perception.social.journal:
                if entry.timestamp < chop_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in self._SUCCESS_SNIPPETS):
                    return True
            return False

        deadline = chop_start + 3.0
        result_snippet: str | None = None
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            result_snippet = _journal_result_seen()
            if _logs_in_pack() > logs_before or result_snippet:
                break

        # The success cliloc can arrive before the item-update packet that
        # actually adds the logs to the pack. If the swing reported success
        # but the logs are not visible yet (and no skip/terminal-failure line
        # fired), grace-poll briefly for the container update so the yield is
        # credited instead of being lost as a phantom skill-check miss.
        #
        # Gate the grace-poll on a STANDALONE success line, NOT on the
        # ``result_snippet`` that drove tree-parking above. ``result_snippet``
        # is only the FIRST ``_RESULT_SNIPPETS`` match in journal order, so on
        # the swing that *strips* a tree — ServUO emits both a success line
        # ("...logs into your backpack") AND a depletion line ("not enough wood
        # here") within the same window — the depletion line can sort first.
        # The old ``result_snippet not in _SKIP_TREE_SNIPPETS`` gate then SKIPS
        # the grace-poll even though a real success line is present: if the log
        # item-update packet lost the race, ``logs_gained == 0`` and the
        # genuinely-successful final chop is mis-booked as a BLOCKED "Failed to
        # chop wood" with zero yield credited. Mirror mine_ore / smelt_ore,
        # which gate purely on a standalone ``_journal_success_seen()`` and so
        # recover the credit regardless of which terminal line landed first.
        if _logs_in_pack() <= logs_before and _success_seen():
            grace_deadline = time.time() + 1.0
            while time.time() < grace_deadline:
                await asyncio.sleep(0.1)
                if _logs_in_pack() > logs_before:
                    break

        # A worn-out hatchet must be surfaced as MISSING_RESOURCE so the planner
        # routes to make_tools / buy_from_vendor (mirroring mine_ore's tool_broke
        # branch). Without this, the destroyed-tool swing (a) was absent from
        # _RESULT_SNIPPETS so it stalled the full 3s deadline, and (b) fell
        # through to the generic "Failed to chop wood" BLOCKED branch with no
        # next_suggestion — and worse, "worn out" is NOT in _SKIP_TREE_SNIPPETS,
        # so a perfectly good tree never got parked while the agent had no
        # hatchet to swing. Check this BEFORE parking the tree as depleted.
        if _tool_broke_seen():
            logger.warning("chop_tool_broke", pos=f"({tx},{ty})")
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="Hatchet worn out — need a replacement hatchet",
                details={"tree": (tx, ty), "tool_broke": True},
            )

        # A depleted / out-of-range tree must be parked in the shared cooldown
        # so _find_nearby_tree skips it next iteration; otherwise the loop pins
        # on the same dead tree and chops nothing for DEPLETED_COOLDOWN seconds.
        if _skip_tree_seen():
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
