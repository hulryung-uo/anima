"""Lumberjacking skill — chop trees for logs."""

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

HATCHET_GRAPHICS = {0x0F43, 0x0F44, 0x0F47, 0x0F48, 0x0F4B, 0x0F4D}

TREE_GRAPHICS = {
    0x0CCA, 0x0CCB, 0x0CCC, 0x0CCD, 0x0CD0, 0x0CD3, 0x0CD6, 0x0CD8,
    0x0CDA, 0x0CDD, 0x0CE0, 0x0CE3, 0x0CE6, 0x0CF8, 0x0CFB, 0x0CFE,
    0x0D01, 0x0D25, 0x0D27, 0x0D35, 0x0D37, 0x0D38, 0x0D42, 0x0D43,
    0x0D58, 0x0D59, 0x0D5A, 0x0D5B, 0x0D94, 0x0D95, 0x0D96, 0x0D97,
    0x0D98, 0x0D99, 0x0D9A, 0x0D9B,
}

LOG_GRAPHICS = {0x1BDD, 0x1BE0}
LUMBERJACK_SKILL_ID = 44
SEARCH_RADIUS = 8  # tiles to search for trees
DEPLETED_COOLDOWN = 1200.0  # seconds before retrying a depleted tree (~20 min)


def _find_nearby_tree(ctx: BrainContext) -> tuple[int, int, int, int] | None:
    """Find a tree within SEARCH_RADIUS tiles, skipping depleted ones.

    Checks BOTH map statics and world items.
    Returns (x, y, z, graphic) or None.
    """
    ss = ctx.perception.self_state
    sx, sy = ss.x, ss.y
    depleted: dict[tuple[int, int], float] = ctx.blackboard.setdefault(
        "depleted_trees", {}
    )
    now = time.time()

    # Check map statics first (most trees are static)
    if ctx.map_reader is not None:
        best = None
        best_dist = SEARCH_RADIUS + 1
        for dy in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
            for dx in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
                dist = max(abs(dx), abs(dy))
                if dist >= best_dist:
                    continue
                tx, ty = sx + dx, sy + dy
                # Skip depleted trees
                dep_time = depleted.get((tx, ty))
                if dep_time and now - dep_time < DEPLETED_COOLDOWN:
                    continue
                tile = ctx.map_reader.get_tile(tx, ty)
                for s in tile.statics:
                    if s.graphic in TREE_GRAPHICS:
                        best = (tx, ty, s.z, s.graphic)
                        best_dist = dist
                        break
        if best:
            return best

    # Fallback: check world items (dynamic trees)
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.graphic in TREE_GRAPHICS:
            dep_time = depleted.get((it.x, it.y))
            if dep_time and now - dep_time < DEPLETED_COOLDOWN:
                continue
            dist = max(abs(it.x - sx), abs(it.y - sy))
            if dist <= SEARCH_RADIUS:
                return (it.x, it.y, it.z, it.graphic)

    return None


class ChopWood(Skill):
    """Chop trees with a hatchet to gather logs."""

    name = "chop_wood"
    category = "gathering"
    description = "Use a hatchet on nearby trees to chop logs."
    required_skill = (LUMBERJACK_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        # Don't chop if near weight limit (logs are heavy)
        if ss.weight_max > 0 and ss.weight >= ss.weight_max - 20:
            return False
        if not _find_hatchet(ctx):
            return False
        return _find_nearby_tree(ctx) is not None

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        backpack = ss.equipment.get(0x15)
        hatchet = _find_hatchet(ctx)

        if not hatchet:
            return SkillResult(success=False, reward=-1.0, message="No hatchet")

        tree = _find_nearby_tree(ctx)
        if not tree:
            return SkillResult(success=False, reward=-1.0, message="No trees nearby")

        tree_x, tree_y, tree_z, tree_graphic = tree
        dist = max(abs(tree_x - ss.x), abs(tree_y - ss.y))

        logger.info(
            "chop_start",
            tree=f"0x{tree_graphic:04X}",
            pos=f"({tree_x},{tree_y},{tree_z})",
            dist=dist,
        )
        feed = ctx.blackboard.get("activity_feed")

        # Walk closer if too far (need to be within 2 tiles)
        if dist > 2:
            if feed:
                feed.publish("skill", f"Walking to tree at ({tree_x},{tree_y})", importance=1)

            # Walk to an adjacent tile, not the tree tile itself
            from anima.action.movement import go_to
            from anima.pathfinding import DIRECTION_DELTAS

            best_adj = None
            best_adj_dist = 999
            for d in range(8):
                dx, dy = DIRECTION_DELTAS[d]
                ax, ay = tree_x + dx, tree_y + dy
                ad = max(abs(ax - ss.x), abs(ay - ss.y))
                if ctx.map_reader:
                    tile = ctx.map_reader.get_tile(ax, ay)
                    can, _ = tile.walkable_z(ss.z)
                    if can and ad < best_adj_dist:
                        best_adj = (ax, ay)
                        best_adj_dist = ad

            if best_adj:
                await go_to(ctx, best_adj[0], best_adj[1])

            # Check if we actually got close enough
            new_dist = max(abs(tree_x - ss.x), abs(tree_y - ss.y))
            if new_dist > 3:
                # Give up on this tree — mark as unreachable
                depleted: dict[tuple[int, int], float] = ctx.blackboard.setdefault(
                    "depleted_trees", {}
                )
                depleted[(tree_x, tree_y)] = time.time()
                logger.info("chop_tree_unreachable", pos=f"({tree_x},{tree_y})", dist=new_dist)
                if feed:
                    feed.publish(
                        "skill", "Can't reach tree, skipping", importance=1,
                    )
                return SkillResult(
                    success=False, reward=-0.5,
                    message=f"Tree at ({tree_x},{tree_y}) unreachable",
                )

        if feed:
            feed.publish("skill", f"Chopping tree at ({tree_x},{tree_y})", importance=2)

        logs_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )

        # Double-click hatchet to activate → server sends target cursor (0x6C)
        ss.pending_target = None
        await ctx.conn.send_packet(build_double_click(hatchet.serial))

        # Wait for target cursor from server
        for _ in range(20):  # up to 2 seconds
            await asyncio.sleep(0.1)
            if ss.pending_target is not None:
                break

        if ss.pending_target is None:
            return SkillResult(success=False, reward=-0.5, message="No target cursor received")

        cursor_id = ss.pending_target.get("cursor_id", 0)
        ss.pending_target = None

        # Target the tree (static target = target_type 1)
        await ctx.conn.send_packet(build_target_response(
            target_type=1, cursor_id=cursor_id,
            x=tree_x, y=tree_y, z=tree_z, graphic=tree_graphic,
        ))
        logger.debug("chop_target_sent", cursor_id=f"0x{cursor_id:08X}")

        # Wait for server response — poll journal for result message
        result_msg = ""
        deadline = time.monotonic() + 6.0
        journal_mark = time.time()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            for entry in ctx.perception.social.recent(count=5):
                if entry.timestamp < journal_mark:
                    continue
                text_lower = entry.text.lower()
                if "logs into your backpack" in text_lower:
                    result_msg = "success"
                    break
                if "not enough wood" in text_lower:
                    result_msg = "depleted"
                    break
                if "fail to produce" in text_lower:
                    result_msg = "fail"
                    break
            if result_msg:
                break

        elapsed = (time.monotonic() - start) * 1000

        # Count logs gained
        logs_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in LOG_GRAPHICS
        )
        logs_gained = logs_after - logs_before

        # Handle depleted tree
        if result_msg == "depleted":
            depleted: dict[tuple[int, int], float] = ctx.blackboard.setdefault(
                "depleted_trees", {}
            )
            depleted[(tree_x, tree_y)] = time.time()
            logger.info("chop_tree_depleted", pos=f"({tree_x},{tree_y})")
            if feed:
                feed.publish(
                    "skill", f"Tree ({tree_x},{tree_y}) depleted", importance=1,
                )
            return SkillResult(
                success=False, reward=-0.5,
                message=f"Tree at ({tree_x},{tree_y}) depleted",
                duration_ms=elapsed,
            )

        # Handle success
        if result_msg == "success" or logs_gained > 0:
            gained = max(logs_gained, 1)
            logger.info("chop_success", logs=gained)
            if feed:
                feed.publish("skill", f"Chopped {gained} logs!", importance=2)
            return SkillResult(
                success=True, reward=5.0 + gained,
                message=f"Chopped {gained} logs",
                skill_gains=[(LUMBERJACK_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )

        # Handle fail (tree still has wood, just bad luck)
        logger.info("chop_fail")
        return SkillResult(
            success=False, reward=-0.5,
            message="Failed to chop, will try again",
            duration_ms=elapsed,
        )


def _find_hatchet(ctx: BrainContext):
    """Find a hatchet in backpack OR equipped (hand slots)."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    backpack = ss.equipment.get(0x15)

    # Check backpack
    if backpack:
        for it in world.items.values():
            if it.container == backpack and it.graphic in HATCHET_GRAPHICS:
                return it

    # Check equipped items (one_handed=0x01, two_handed=0x02)
    for layer in (0x01, 0x02):
        eq_serial = ss.equipment.get(layer)
        if eq_serial:
            it = world.items.get(eq_serial)
            if it and it.graphic in HATCHET_GRAPHICS:
                return it

    return None
