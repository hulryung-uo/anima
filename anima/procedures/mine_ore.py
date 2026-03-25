"""MineOre procedure — use pickaxe on mountain/cave tiles to gather ore."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.actions.target import use_on_target, wait_for_target
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


class MineOre(Procedure):
    name = "mine_ore"
    description = "Use pickaxe on a mountain/cave tile to mine ore."

    async def can_start(self, ctx: AgentContext) -> bool:
        if not find_in_backpack(ctx, MINING_TOOL_GRAPHICS):
            return False
        return _find_mineable_tile(ctx) is not None

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
        _depleted_flag = {"hit": False}

        def _check_speech(_topic: str, data: dict) -> None:
            text = data.get("text", "")
            tl = text.lower()
            if ("no metal here" in tl or "no ore here" in tl
                    or "target cannot be seen" in tl or "too far away" in tl):
                _depleted_flag["hit"] = True

        sub = None
        if ctx.bus:
            sub = ctx.bus.subscribe("avatar.speech_heard", _check_speech)
            # Also subscribe to cliloc speech
            sub2 = ctx.bus.subscribe("avatar.speech_cliloc", _check_speech)

        await asyncio.sleep(3.0)

        if sub and ctx.bus:
            ctx.bus.unsubscribe(sub)
            ctx.bus.unsubscribe(sub2)  # type: ignore[possibly-undefined]

        if _depleted_flag["hit"]:
            # Mark tile as depleted in blackboard
            depleted = ctx.blackboard.setdefault("depleted_mines", {})
            depleted[(tx, ty)] = _time.time()
            logger.info("mine_depleted", pos=f"({tx},{ty})")
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"Vein depleted at ({tx},{ty})",
                next_suggestion="mine_ore",
                details={"tile": (tx, ty), "depleted": True},
            )

        # Count ore after
        ore_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )
        ore_gained = ore_after - ore_before

        if ore_gained > 0:
            # Drop ore — Razor-style auto-stack: drop onto existing ground ore
            from anima.client.packets import build_drop_item, build_pick_up
            for item in world.items.values():
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
                    break

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
