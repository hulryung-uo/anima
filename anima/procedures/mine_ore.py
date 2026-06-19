"""MineOre procedure — use pickaxe on mountain/cave tiles to gather ore."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.target import use_on_target
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.gathering.mine import (
    ORE_GRAPHICS,
    PICKAXE_GRAPHICS,
    _find_mineable_tile,
)

SHOVEL_GRAPHICS = {0x0F39}
MINING_TOOL_GRAPHICS = PICKAXE_GRAPHICS | SHOVEL_GRAPHICS

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


def _trip_bank(ctx: "AgentContext", tx: int, ty: int) -> tuple[int, int]:
    """Mark the ServUO ore bank at (tx, ty) as depleted.

    Writes to both the legacy `depleted_banks` dict (for tests and
    contexts that don't install a CircuitBreaker) and the breaker if
    present on the blackboard. Returns the bank key for logging.
    """
    import time as _t

    from anima.skills.gathering.mine import _bank_key

    bk = _bank_key(tx, ty)
    depleted_banks = ctx.blackboard.setdefault("depleted_banks", {})
    depleted_banks[bk] = _t.time()
    breaker = ctx.blackboard.get("_bank_breaker")
    if breaker is not None:
        breaker.trip(bk)
    return bk


class MineOre(Procedure):
    timeout_s = 600.0  # full gather tours run long — generous anti-freeze cap
    name = "mine_ore"
    description = "Use pickaxe on a mountain/cave tile to mine ore."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, MINING_TOOL_GRAPHICS):
            return False
        # Only consider tiles within actual mining range (SEARCH_RADIUS=2),
        # not the wider MOVE_RADIUS scan — otherwise we try to mine tiles
        # that are too far away and get stuck in a loop.
        from anima.skills.gathering.mine import SEARCH_RADIUS
        tile = _find_mineable_tile(ctx)
        if tile is None:
            return False
        tx, ty = tile[0], tile[1]
        ss = ctx.perception.self_state
        return max(abs(tx - ss.x), abs(ty - ss.y)) <= SEARCH_RADIUS

    async def diagnose(self, ctx: AgentContext) -> str | None:
        if not find_in_backpack(ctx, MINING_TOOL_GRAPHICS):
            return "no mining tool (pickaxe or shovel)"
        if _find_mineable_tile(ctx) is None:
            return "no mineable tile nearby"
        return None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)

        # Find pickaxe
        tools = find_in_backpack(ctx, MINING_TOOL_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no mining tool (pickaxe or shovel)",
            )

        # Find mineable tile
        tile_info = _find_mineable_tile(ctx)
        if tile_info is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no mineable tile nearby",
            )

        tx, ty, tz, graphic, is_static = tile_info

        # Check interrupt: HP low
        if ss.hits_max > 0 and ss.hits < ss.hits_max * 0.3:
            return ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message="HP too low",
            )

        # Count ore before
        ore_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )

        # Use pickaxe on tile
        result = await use_on_target(
            ctx, tools[0].serial,
            x=tx, y=ty, z=tz,
            graphic=graphic if is_static else 0,
        )
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        # Wait for mining animation + check for "no metal here" via bus
        import time as _time
        mine_start = _time.time()
        _mine_flags = {
            "depleted": False,
            "los_fail": False,
            "too_far": False,
            "tool_broke": False,
        }

        def _check_speech(_topic: str, data: dict) -> None:
            text = data.get("text", "")
            tl = text.lower()
            if "no metal here" in tl or "no ore here" in tl:
                _mine_flags["depleted"] = True
            elif "target cannot be seen" in tl:
                _mine_flags["los_fail"] = True
            elif "too far away" in tl:
                _mine_flags["too_far"] = True
            # 1044038 "You have worn out your tool!" — ServUO HarvestSystem
            # deletes the pickaxe/shovel when UsesRemaining hits 0.
            elif "worn out" in tl:
                _mine_flags["tool_broke"] = True

        sub = None
        if ctx.bus:
            sub = ctx.bus.subscribe("avatar.speech_heard", _check_speech)
            # Also subscribe to cliloc speech
            sub2 = ctx.bus.subscribe("avatar.speech_cliloc", _check_speech)

        # Event-driven cadence: instead of a flat 3s nap per swing, poll
        # until the swing resolves — new ore in the pack OR a mining-result
        # journal line — and only burn the full window on a true timeout.
        # Snippets are matched (lower-cased substring) against the real
        # ServUO mining result clilocs so every resolved swing breaks the
        # poll immediately. Missing the "someone has gotten to the metal"
        # (503042 race-loss) and "moved too far away" (503041) lines made
        # those common busy-bank swings stall the whole 3.5s deadline,
        # which is the dominant dead-time sink in low-skill gather windows.
        _result_snippets = (
            "you dig some",
            "you loosen some rocks",
            "there is no metal here",
            "someone has gotten to the metal",
            "can't mine",
            "too far away",
            "moved too far away",
            "worn out your tool",
        )

        def _ore_in_pack() -> int:
            return sum(
                it.amount for it in world.items.values()
                if it.container == backpack and it.graphic in ORE_GRAPHICS
            )

        def _journal_result_seen() -> bool:
            for entry in ctx.perception.social.journal:
                if entry.timestamp < mine_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in _result_snippets):
                    return True
            return False

        deadline = mine_start + 3.5
        while _time.time() < deadline:
            await asyncio.sleep(0.2)
            if (
                _ore_in_pack() > ore_before
                or any(_mine_flags.values())
                or _journal_result_seen()
            ):
                break

        if sub and ctx.bus:
            ctx.bus.unsubscribe(sub)
            ctx.bus.unsubscribe(sub2)  # type: ignore[possibly-undefined]

        # A worn-out tool can arrive on the journal even when no bus is wired
        # (the bus is optional). Reconcile from the journal so the restock
        # signal fires regardless of transport.
        if not _mine_flags["tool_broke"]:
            for entry in ctx.perception.social.journal:
                if entry.timestamp < mine_start:
                    continue
                if "worn out" in entry.text.lower():
                    _mine_flags["tool_broke"] = True
                    break

        if _mine_flags["tool_broke"]:
            # The pickaxe is gone — this is NOT a depleted bank and NOT a
            # skill-check miss. Surfacing it as MISSING_RESOURCE (mirroring
            # craft_blacksmith's tool_broke path) lets the planner route to
            # make_tools / buy_from_vendor. Re-suggesting mine_ore here would
            # loop against a perfectly good vein with no tool to swing.
            logger.warning("mine_tool_broke", pos=f"({tx},{ty})")
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="Mining tool worn out — need a replacement pickaxe/shovel",
                details={"tile": (tx, ty), "tool_broke": True},
            )

        if _mine_flags["depleted"]:
            bk = _trip_bank(ctx, tx, ty)
            logger.info("mine_bank_depleted", pos=f"({tx},{ty})", bank=str(bk))
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"Vein depleted at ({tx},{ty})",
                next_suggestion="mine_ore",
                details={"tile": (tx, ty), "bank": bk, "depleted": True},
            )

        if _mine_flags["too_far"]:
            logger.info("mine_too_far", pos=f"({tx},{ty})", player=f"({ss.x},{ss.y})")
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"Mining target ({tx},{ty}) too far from player ({ss.x},{ss.y})",
            )

        if _mine_flags["los_fail"]:
            bk = _trip_bank(ctx, tx, ty)
            logger.info("mine_los_fail", pos=f"({tx},{ty})", bank=str(bk))
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Cannot see mining target at ({tx},{ty})",
            )

        # Count ore after
        ore_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )
        ore_gained = ore_after - ore_before

        if ore_gained > 0:
            # Drop ore — Razor-style auto-stack: drop onto existing ground ore.
            # AOS-era ServUO REJECTS world drops onto a tile occupied by a
            # mobile (incl. the dropper): the ore silently bounces back into
            # the pack. Verify the drop landed before registering a ground
            # pile, and stop trying once a bounce is observed this session —
            # backpack ore flows to smelt_ore directly.
            from anima.client.packets import build_drop_item, build_pick_up
            drop_verified = False
            ground_drops_broken = ctx.blackboard.get("_ground_drop_bounces", False)
            # Once a bounce is observed, skip the whole pick_up/drop dance
            # up-front — keep ore in pack; smelt_ore picks it up.
            drop_candidates = (
                () if ground_drops_broken else tuple(world.items.values())
            )
            for item in drop_candidates:
                if item.container == backpack and item.graphic in ORE_GRAPHICS:
                    # Find same-type ore on ground nearby to stack on
                    stack_target = None
                    for ground_item in world.items.values():
                        if (ground_item.serial != item.serial
                                and ground_item.container == 0
                                and ground_item.graphic == item.graphic
                                and ground_item.hue == item.hue
                                and max(abs(ground_item.x - ss.x), abs(ground_item.y - ss.y)) <= 2):
                            stack_target = ground_item
                            break

                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.3)

                    if stack_target:
                        # Drop onto existing item → server auto-stacks
                        await ctx.conn.send_packet(
                            build_drop_item(item.serial, container=stack_target.serial)
                        )
                    else:
                        # No existing ore → drop at feet
                        await ctx.conn.send_packet(
                            build_drop_item(item.serial, ss.x, ss.y, ss.z)
                        )
                    await asyncio.sleep(0.3)

                    # Verify: did the item actually leave the backpack?
                    still_packed = world.items.get(item.serial)
                    if (still_packed is not None
                            and still_packed.container == backpack
                            and not stack_target):
                        ctx.blackboard["_ground_drop_bounces"] = True
                        logger.info(
                            "mine_ore_drop_bounced",
                            serial=f"0x{item.serial:08X}",
                            note="server rejected ground drop; keeping ore in pack",
                        )
                    else:
                        drop_verified = True

                    # If this ore hue was previously blacklisted, mark as junk
                    # so smelt_ore won't waste a cycle trying it
                    unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())
                    if item.hue in unsmelable:
                        junk = ctx.blackboard.setdefault("_junk_ore_serials", set())
                        junk.add(item.serial)
                        if stack_target:
                            junk.add(stack_target.serial)
                    break

            from anima.skills.gathering.mine import (
                SAME_SPOT_MINE_LIMIT,
                _bank_key,
            )

            # Only register a ground pile the collect leg can actually find.
            # Unverified drops used to create PHANTOM piles ("pile empty at
            # target") that wasted a pickup walk every cycle. The expedition
            # state machine (phase/home_base) still advances either way.
            expedition = ctx.blackboard.get("expedition")
            if expedition is not None:
                expedition.note_ore_mined(
                    ss.x, ss.y, _bank_key(ss.x, ss.y),
                    ground_pile=drop_verified,
                )

            # Track consecutive successful mines on the same *mined tile*
            # (tx, ty), not the player's footing (ss.x, ss.y). The agent
            # stands still and swings at a target up to SEARCH_RADIUS tiles
            # away, so the player tile and the mined tile can fall into
            # different ServUO 8x8 ore banks at a bank boundary. Keying the
            # voluntary cooldown on the player tile pauses the wrong bank —
            # the agent's footing bank — while the actually over-mined bank
            # stays selectable and keeps getting hammered. Key on the mined
            # tile so the pause lands on the bank that is being depleted,
            # consistent with _trip_bank (which uses _bank_key(tx, ty)).
            #
            # After SAME_SPOT_MINE_LIMIT hits, pause that bank so the agent
            # walks to a fresh spot — ServUO banks respawn faster than
            # natural depletion in low-skill windows.
            same_spot = ctx.blackboard.setdefault(
                "_mine_same_spot", {"pos": None, "count": 0}
            )
            cur_pos = (tx, ty)
            if same_spot.get("pos") == cur_pos:
                same_spot["count"] = same_spot.get("count", 0) + 1
            else:
                same_spot["pos"] = cur_pos
                same_spot["count"] = 1
            if same_spot["count"] >= SAME_SPOT_MINE_LIMIT:
                voluntary_cd = ctx.blackboard.setdefault(
                    "_voluntary_cooldown_banks", {}
                )
                bk = _bank_key(tx, ty)
                voluntary_cd[bk] = time.time()
                logger.info(
                    "mine_same_spot_pause",
                    bank=str(bk),
                    pos=f"({tx},{ty})",
                    after=same_spot["count"],
                )
                same_spot["pos"] = None
                same_spot["count"] = 0

            return ProcedureResult(
                success=True,
                message=f"Mined {ore_gained} ore",
                next_suggestion="mine_ore",
                details={"ore_count": ore_gained, "tile": (tx, ty)},
            )
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Mining failed (skill check)",
                next_suggestion="mine_ore",
                details={"tile": (tx, ty)},
            )
