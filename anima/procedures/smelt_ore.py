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

        unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())

        has_ore = any(
            it.graphic in ORE_GRAPHICS and it.hue not in unsmelable
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_ore:
            # Also check ground nearby (excluding junk serials)
            junk = ctx.blackboard.get("_junk_ore_serials", set())
            has_ore = any(
                it.graphic in ORE_GRAPHICS and it.container == 0
                and it.hue not in unsmelable
                and it.serial not in junk
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

        # Find ore (skip hues we've proven unsmelable)
        unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())
        ore = None
        for item in world.items.values():
            if (item.container == backpack and item.graphic in ORE_GRAPHICS
                    and item.hue not in unsmelable):
                ore = item
                break

        if not ore:
            # Pick up from ground (skip unsmelable hues and junk serials)
            junk = ctx.blackboard.get("_junk_ore_serials", set())
            from anima.client.packets import build_drop_item, build_pick_up
            for item in world.items.values():
                if (item.graphic in ORE_GRAPHICS and item.container == 0
                        and item.hue not in unsmelable
                        and item.serial not in junk
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

        # Listen for server feedback during smelting
        _smelt_flags = {"not_enough": False}

        def _on_speech(_topic: str, data: dict) -> None:
            text = data.get("text", "")
            if "not enough metal" in text.lower():
                _smelt_flags["not_enough"] = True

        sub1 = sub2 = None
        if ctx.bus:
            sub1 = ctx.bus.subscribe("avatar.speech_heard", _on_speech)
            sub2 = ctx.bus.subscribe("avatar.speech_cliloc", _on_speech)

        # Smelt: double-click ore → target forge
        if forge_dyn:
            result = await use_on_object(ctx, ore.serial, forge_dyn[3])
        else:
            fx, fy, fz, fg = forge_sta  # type: ignore[misc]
            result = await use_on_target(ctx, ore.serial, fx, fy, fz, graphic=fg)

        if not result.success:
            if ctx.bus:
                if sub1: ctx.bus.unsubscribe(sub1)
                if sub2: ctx.bus.unsubscribe(sub2)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        await asyncio.sleep(2.0)

        if ctx.bus:
            if sub1: ctx.bus.unsubscribe(sub1)
            if sub2: ctx.bus.unsubscribe(sub2)

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )
        ingots_gained = ingots_after - ingots_before

        ore_hue = ore.hue  # track which hue we attempted

        if ingots_gained > 0:
            # Reset fail counter for this ore hue on success
            fail_counts = ctx.blackboard.get("_smelt_fail_counts", {})
            fail_counts.pop(ore_hue, None)
            return ProcedureResult(
                success=True,
                message=f"Smelted {ingots_gained} ingots",
                next_suggestion="smelt_ore",
                details={"ingots": ingots_gained},
            )

        # --- Smelting failed ---
        # "not enough metal" = definitive signal → blacklist immediately.
        # Otherwise use 3-strike counter (could be a skill check failure).
        immediate_blacklist = _smelt_flags["not_enough"]

        fail_counts = ctx.blackboard.setdefault("_smelt_fail_counts", {})
        fail_count = fail_counts.get(ore_hue, 0) + 1
        fail_counts[ore_hue] = fail_count

        if immediate_blacklist or fail_count >= 3:
            fail_counts.pop(ore_hue, None)
            unsmelable_set = ctx.blackboard.setdefault(
                "_unsmelable_ore_hues", set()
            )
            unsmelable_set.add(ore_hue)

            # Drop only ore of this hue (not all ore)
            from anima.client.packets import build_drop_item, build_pick_up
            dropped = 0
            for item in list(world.items.values()):
                if (item.container == backpack
                        and item.graphic in ORE_GRAPHICS
                        and item.hue == ore_hue):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(item.serial, ss.x, ss.y, ss.z)
                    )
                    await asyncio.sleep(0.3)
                    dropped += 1
            # Mark dropped ore as junk so planner won't pick them up
            junk = ctx.blackboard.setdefault("_junk_ore_serials", set())
            for item in world.items.values():
                if (item.container == 0 and item.graphic in ORE_GRAPHICS
                        and item.hue == ore_hue
                        and max(abs(item.x - ss.x), abs(item.y - ss.y)) <= 2):
                    junk.add(item.serial)

            reason_str = "not enough metal" if immediate_blacklist else f"{fail_count} failures"
            logger.info(
                "smelt_gave_up_ore",
                dropped=dropped, hue=ore_hue, reason=reason_str,
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Ore hue {ore_hue} unsmelable ({reason_str}), dropped {dropped} stacks",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Smelting failed ({fail_count}/3)",
        )
