"""High-level movement actions: go_to, wander."""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_walk_request
from anima.pathfinding import DIRECTION_DELTAS, direction_to, find_path

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()


async def go_to(ctx: BrainContext, target_x: int, target_y: int) -> bool:
    """Pathfind and walk step-by-step to (target_x, target_y).

    Returns True if destination reached (within 1 tile), False if failed.
    Sends walk packets and waits for confirmation, with retry on deny.
    """
    ss = ctx.perception.self_state
    max_attempts = 30  # max walk steps before giving up
    attempts = 0

    while attempts < max_attempts and ctx.conn.connected:
        sx, sy = ss.x, ss.y
        dist = max(abs(target_x - sx), abs(target_y - sy))
        if dist <= 1:
            return True

        # Wait until we can walk
        for _ in range(20):
            if ctx.walker.can_walk():
                break
            await asyncio.sleep(0.1)
        else:
            return False

        # Pathfind
        denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)
        sz = ss.z
        path = find_path(
            ctx.map_reader, sx, sy, target_x, target_y,
            denied_tiles=denied, current_z=sz,
        )
        if not path:
            return False

        # Send walk packet for first step
        next_x, next_y = path[0]
        direction = direction_to(sx, sy, next_x, next_y)

        ctx.walker._pending_step_tile = (next_x, next_y)
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
        )
        attempts += 1

        # Wait for server to confirm/deny
        await asyncio.sleep(ctx.cfg.movement.walk_delay_ms / 1000.0 + 0.05)

    return max(abs(target_x - ss.x), abs(target_y - ss.y)) <= 1


async def wander_action(ctx: BrainContext) -> Status:
    """Pick a nearby walkable tile and take one step toward it.

    Uses smart scoring: prefers unvisited tiles, avoids denied tiles,
    and biases toward known locations when there's no active goal.
    """
    from anima.brain.behavior_tree import Status

    sx = ctx.perception.self_state.x
    sy = ctx.perception.self_state.y

    if not ctx.walker.can_walk():
        return Status.RUNNING

    # If too many consecutive denials, try to escape instead of just cooling down
    if ctx.walker.consecutive_denials >= 5:
        logger.info("wander_stuck", denials=ctx.walker.consecutive_denials)
        ctx.walker.consecutive_denials = 0
        if ctx.map_reader is not None:
            escaped = await _escape_stuck(ctx)
            if escaped:
                return Status.SUCCESS
        # Couldn't escape — cooldown
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + 5000
        )
        feed = ctx.blackboard.get("activity_feed")
        if feed:
            feed.publish("movement", "Stuck — cooling down", importance=2)
        return Status.FAILURE

    # Track visited tiles
    now = time.time()
    visited: dict[tuple[int, int], float] = ctx.blackboard.setdefault("visited_tiles", {})
    visited[(sx, sy)] = now

    # Prune old entries
    if len(visited) > 200:
        sorted_tiles = sorted(visited.items(), key=lambda t: t[1])
        for tile, _ in sorted_tiles[: len(visited) - 200]:
            del visited[tile]

    # Collect dynamic obstacles once
    dynamic_blocked = _impassable_world_items(ctx)

    # Score each direction
    candidates: list[tuple[int, float]] = []

    sz = ctx.perception.self_state.z

    for direction in range(8):
        dx, dy = DIRECTION_DELTAS[direction]
        nx, ny = sx + dx, sy + dy

        if ctx.map_reader is not None:
            tile = ctx.map_reader.get_tile(nx, ny)
            can_walk, _ = tile.walkable_z(sz)
            if not can_walk:
                continue

        if ctx.walker.is_tile_denied(nx, ny):
            continue

        if (nx, ny) in dynamic_blocked:
            continue

        score = 1.0

        # Prefer unvisited tiles
        if (nx, ny) not in visited:
            score += 3.0
        else:
            age = now - visited[(nx, ny)]
            score += min(age / 60.0, 2.0)

        candidates.append((direction, score))

    if not candidates:
        # All 8 adjacent tiles blocked — try pathfinding to a farther open tile
        if ctx.map_reader is not None:
            escaped = await _escape_stuck(ctx)
            if escaped:
                return Status.SUCCESS
        return Status.FAILURE

    # Bias toward nearest known location if no active goal
    if not ctx.blackboard.get("current_goal"):
        try:
            from anima.world_knowledge import nearest_locations
            nearest = nearest_locations(sx, sy, count=1)
            if nearest:
                loc, dist = nearest[0]
                if dist > 5:
                    for i, (d, score) in enumerate(candidates):
                        ddx, ddy = DIRECTION_DELTAS[d]
                        new_dist = max(abs(sx + ddx - loc.x), abs(sy + ddy - loc.y))
                        if new_dist < dist:
                            candidates[i] = (d, score + 1.5)
        except ImportError:
            pass

    # Weighted random selection
    total = sum(s for _, s in candidates)
    r = random.random() * total
    cumulative = 0.0
    direction = candidates[0][0]
    for d, s in candidates:
        cumulative += s
        if cumulative >= r:
            direction = d
            break

    dx, dy = DIRECTION_DELTAS[direction]
    nx, ny = sx + dx, sy + dy

    # Record pending step for denial tracking
    ctx.walker._pending_step_tile = (nx, ny)

    # Send walk request (direction includes turn + move)
    seq = ctx.walker.next_sequence()
    fastwalk = ctx.walker.pop_fast_walk_key()
    pkt = build_walk_request(direction, seq, fastwalk)
    await ctx.conn.send_packet(pkt)
    ctx.walker.steps_count += 1
    ctx.walker.last_step_time = (
        asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
    )

    return Status.SUCCESS


def _impassable_world_items(ctx: BrainContext) -> set[tuple[int, int]]:
    """Collect (x, y) of ground-level world items that may block movement."""
    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.serial & 0x40000000 == 0:
            continue
        blocked.add((it.x, it.y))
    return blocked


async def _escape_stuck(ctx: BrainContext) -> bool:
    """Try to pathfind to an open tile when all 8 adjacent tiles are blocked.

    Searches outward in a spiral pattern for a walkable tile, then
    uses pathfinding (with larger max_steps) to get there.
    """
    ss = ctx.perception.self_state
    sx, sy, sz = ss.x, ss.y, ss.z
    denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)

    # Search for an open tile in expanding radius
    for radius in range(2, 25):
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue  # only check the ring at this radius
                tx, ty = sx + dx, sy + dy
                if (tx, ty) in denied:
                    continue
                if ctx.map_reader is None:
                    continue
                tile = ctx.map_reader.get_tile(tx, ty)
                can, _ = tile.walkable_z(sz)
                if not can:
                    continue

                # Found open tile — pathfind there
                path = find_path(
                    ctx.map_reader, sx, sy, tx, ty,
                    max_steps=500, denied_tiles=denied, current_z=sz,
                )
                if path:
                    logger.info(
                        "escape_stuck",
                        target=f"({tx},{ty})",
                        dist=radius,
                        path_len=len(path),
                    )
                    feed = ctx.blackboard.get("activity_feed")
                    if feed:
                        feed.publish(
                            "movement",
                            f"Escaping stuck area → ({tx},{ty})",
                            importance=2,
                        )
                    # Set as move target so _step_toward takes over
                    ctx.blackboard["move_target"] = (tx, ty)
                    ctx.blackboard.pop("current_goal", None)
                    return True

    return False
