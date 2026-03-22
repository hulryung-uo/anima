"""Smelting skill — convert ore into ingots at a forge.

Flow: double-click ore → target forge (static or dynamic item).
ServUO forge IDs: 4017, 6522-6569, 0x2DD8, 0xA531, 0xA535.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_target_response
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

ORE_GRAPHICS = {0x19B7, 0x19B8, 0x19B9, 0x19BA}
INGOT_GRAPHICS = {0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1}
MINING_SKILL_ID = 45

# Forge item IDs (from DefBlacksmithy.cs CheckAnvilAndForge)
FORGE_ITEM_IDS = {4017} | set(range(6522, 6570)) | {0x2DD8, 0xA531, 0xA535}

# Forge static tile IDs (statics use graphic | 0x4000 internally,
# but map reader returns raw graphic without the flag)
FORGE_STATIC_IDS = FORGE_ITEM_IDS


_FORGE_SEARCH_RANGE = 12


def _find_forge_dynamic(ctx: "BrainContext") -> tuple[int, int, int, int] | None:
    """Find a forge from dynamic world items. Returns (x, y, z, serial)."""
    ss = ctx.perception.self_state
    for it in ctx.perception.world.nearby_items(ss.x, ss.y, distance=_FORGE_SEARCH_RANGE):
        if it.graphic in FORGE_ITEM_IDS:
            return (it.x, it.y, it.z, it.serial)
    return None


def _find_forge_static(ctx: "BrainContext") -> tuple[int, int, int, int] | None:
    """Find a forge from map statics. Returns (x, y, z, graphic)."""
    if ctx.map_reader is None:
        return None
    ss = ctx.perception.self_state
    for dy in range(-_FORGE_SEARCH_RANGE, _FORGE_SEARCH_RANGE + 1):
        for dx in range(-_FORGE_SEARCH_RANGE, _FORGE_SEARCH_RANGE + 1):
            tx, ty = ss.x + dx, ss.y + dy
            tile = ctx.map_reader.get_tile(tx, ty)
            for s in tile.statics:
                if s.graphic in FORGE_STATIC_IDS:
                    return (tx, ty, s.z, s.graphic)
    return None


class SmeltOre(Skill):
    """Smelt ore into ingots at a nearby forge."""

    name = "smelt_ore"
    category = "crafting"
    description = "Double-click ore and target a forge to smelt into ingots."
    required_skill = (MINING_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
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
            return False

        # Check for forge — dynamic items or map statics
        return (
            _find_forge_dynamic(ctx) is not None
            or _find_forge_static(ctx) is not None
        )

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()
        backpack = ss.equipment.get(0x15)

        # Find ore in backpack
        ore = None
        for item in world.items.values():
            if item.container == backpack and item.graphic in ORE_GRAPHICS:
                ore = item
                break

        if not ore:
            return SkillResult(success=False, reward=-1.0, message="No ore")

        # Find forge — must be within 1 tile for LOS
        forge_dyn = _find_forge_dynamic(ctx)
        forge_sta = _find_forge_static(ctx)

        if not forge_dyn and not forge_sta:
            return SkillResult(success=False, reward=-1.0, message="No forge nearby")

        # Walk to forge if too far (need to be adjacent)
        if forge_dyn:
            fx, fy = forge_dyn[0], forge_dyn[1]
        else:
            fx, fy = forge_sta[0], forge_sta[1]  # type: ignore[index]
        dist = max(abs(fx - ss.x), abs(fy - ss.y))
        if dist > 1:
            from anima.action.movement import go_to
            logger.info("smelt_walking_to_forge", pos=f"({fx},{fy})", dist=dist)
            arrived = await go_to(ctx, fx, fy)
            if not arrived:
                return SkillResult(
                    success=False, reward=0.0,
                    message=f"Could not reach forge ({fx},{fy})",
                )
            # Re-find forge from new position
            forge_dyn = _find_forge_dynamic(ctx)
            forge_sta = _find_forge_static(ctx)
            if not forge_dyn and not forge_sta:
                return SkillResult(success=False, reward=-1.0, message="Lost forge")

        # Count ingots before
        ingots_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        # Double-click ore → opens target cursor
        ss.pending_target = None
        await ctx.conn.send_packet(build_double_click(ore.serial))

        # Wait for server to send target cursor
        for _ in range(20):
            if ss.pending_target is not None:
                break
            await asyncio.sleep(0.1)

        if ss.pending_target is None:
            return SkillResult(success=False, reward=-1.0, message="No target cursor")

        cursor_id = ss.pending_target.get("cursor_id", 0)
        ss.pending_target = None

        # Target the forge
        if forge_dyn:
            fx, fy, fz, fserial = forge_dyn
            await ctx.conn.send_packet(build_target_response(
                target_type=0,
                cursor_id=cursor_id,
                serial=fserial,
                x=fx, y=fy, z=fz, graphic=0,
            ))
        else:
            fx, fy, fz, fgraphic = forge_sta  # type: ignore[misc]
            await ctx.conn.send_packet(build_target_response(
                target_type=1,
                cursor_id=cursor_id,
                x=fx, y=fy, z=fz, graphic=fgraphic,
            ))

        logger.info("smelt_targeting_forge", pos=f"({fx},{fy})")

        # Wait for smelting result
        await asyncio.sleep(2.0)

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        elapsed = (time.monotonic() - start) * 1000
        ingots_gained = ingots_after - ingots_before

        if ingots_gained > 0:
            reward = 5.0 + ingots_gained * 0.5
            logger.info("smelt_success", ingots=ingots_gained)
            return SkillResult(
                success=True, reward=reward,
                message=f"Smelted {ingots_gained} ingots",
                skill_gains=[(MINING_SKILL_ID, 0.05)],
                duration_ms=elapsed,
            )
        else:
            return SkillResult(
                success=False, reward=-0.5,
                message="Smelting failed",
                duration_ms=elapsed,
            )
