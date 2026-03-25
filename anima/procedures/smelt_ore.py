"""SmeltOre procedure — convert ore into ingots at a forge."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.actions.target import use_on_object, use_on_target
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.crafting.smelt import (
    INGOT_GRAPHICS,
    ORE_GRAPHICS,
    _find_forge_dynamic,
    _find_forge_static,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class SmeltOre(Procedure):
    name = "smelt_ore"
    description = "Double-click ore and target a forge to smelt into ingots."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        has_ore = any(
            it.graphic in ORE_GRAPHICS
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_ore:
            # Also check ground nearby
            has_ore = any(
                it.graphic in ORE_GRAPHICS and it.container == 0
                and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2
                for it in world.items.values()
            )
        if not has_ore:
            return False

        return _find_forge_dynamic(ctx) is not None or _find_forge_static(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)

        # Find ore
        ore = None
        for item in world.items.values():
            if item.container == backpack and item.graphic in ORE_GRAPHICS:
                ore = item
                break

        if not ore:
            # Pick up from ground
            from anima.client.packets import build_drop_item, build_pick_up
            for item in world.items.values():
                if (item.graphic in ORE_GRAPHICS and item.container == 0
                        and max(abs(item.x - ss.x), abs(item.y - ss.y)) <= 2):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(item.serial, 0xFFFF, 0xFFFF, 0, backpack)
                    )
                    await asyncio.sleep(0.5)
                    ore = item
                    break

        if not ore:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no ore",
            )

        # Find forge
        forge_dyn = _find_forge_dynamic(ctx)
        forge_sta = _find_forge_static(ctx)
        if not forge_dyn and not forge_sta:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no forge nearby",
            )

        # Walk to forge if needed
        if forge_dyn:
            fx, fy = forge_dyn[0], forge_dyn[1]
        else:
            fx, fy = forge_sta[0], forge_sta[1]  # type: ignore[index]

        dist = max(abs(fx - ss.x), abs(fy - ss.y))
        if dist > 1:
            from anima.action.movement import go_to
            arrived = await go_to(ctx, fx, fy)
            if not arrived:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"could not reach forge ({fx},{fy})",
                )
            forge_dyn = _find_forge_dynamic(ctx)
            forge_sta = _find_forge_static(ctx)

        # Count ingots before
        ingots_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        # Smelt: double-click ore → target forge
        if forge_dyn:
            result = await use_on_object(ctx, ore.serial, forge_dyn[3])
        else:
            fx, fy, fz, fg = forge_sta  # type: ignore[misc]
            result = await use_on_target(ctx, ore.serial, fx, fy, fz, graphic=fg)

        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        await asyncio.sleep(2.0)

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )
        ingots_gained = ingots_after - ingots_before

        if ingots_gained > 0:
            # Reset fail counter for this ore type on success
            ctx.blackboard.pop("_smelt_fail_count", None)
            return ProcedureResult(
                success=True,
                message=f"Smelted {ingots_gained} ingots",
                next_suggestion="smelt_ore",
                details={"ingots": ingots_gained},
            )
        else:
            # Track consecutive smelt failures — some ore can't be smelted
            fail_count = ctx.blackboard.get("_smelt_fail_count", 0) + 1
            ctx.blackboard["_smelt_fail_count"] = fail_count

            if fail_count >= 3:
                # Give up on this ore — drop it on the ground
                ctx.blackboard["_smelt_fail_count"] = 0
                from anima.client.packets import build_drop_item, build_pick_up
                dropped = 0
                for item in list(world.items.values()):
                    if item.container == backpack and item.graphic in ORE_GRAPHICS:
                        await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                        await asyncio.sleep(0.3)
                        await ctx.conn.send_packet(
                            build_drop_item(item.serial, ss.x, ss.y, ss.z)
                        )
                        await asyncio.sleep(0.3)
                        dropped += 1
                logger.info("smelt_gave_up_ore", dropped=dropped, fails=fail_count)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.PERMANENT,
                    message=f"Ore unsmelable, dropped {dropped} stacks",
                )

            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Smelting failed ({fail_count}/3)",
            )
