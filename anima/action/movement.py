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

_DIR_NAMES = {
    0: "N", 1: "NE", 2: "E", 3: "SE", 4: "S", 5: "SW", 6: "W", 7: "NW",
}


async def go_to(ctx: BrainContext, target_x: int, target_y: int) -> bool:
    """Pathfind and walk step-by-step to (target_x, target_y).

    Returns True if destination reached (within 1 tile), False if failed.
    Calculates a path once, follows it step by step. On denial,
    records the blocked tile and recalculates a new route around it.
    """
    ss = ctx.perception.self_state
    max_steps = 50
    max_recalc = 5  # max path recalculations before giving up
    step_delay = ctx.cfg.movement.walk_delay_ms / 1000.0

    dist = max(abs(target_x - ss.x), abs(target_y - ss.y))
    logger.info(
        "go_to_start",
        pos=f"({ss.x},{ss.y},{ss.z})",
        target=f"({target_x},{target_y})",
        dist=dist,
    )

    steps_taken = 0
    recalcs = 0
    path: list[tuple[int, int]] = []

    while steps_taken < max_steps and ctx.conn.connected:
        sx, sy = ss.x, ss.y
        remaining = max(abs(target_x - sx), abs(target_y - sy))
        if remaining <= 1:
            logger.info("go_to_arrived", pos=f"({sx},{sy})", steps=steps_taken)
            return True

        # Calculate path if we don't have one
        if not path:
            if recalcs >= max_recalc:
                logger.info(
                    "go_to_give_up", pos=f"({sx},{sy})",
                    target=f"({target_x},{target_y})", recalcs=recalcs,
                )
                return False
            denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)
            path = find_path(
                ctx.map_reader, sx, sy, target_x, target_y,
                denied_tiles=denied, current_z=ss.z,
            )
            recalcs += 1
            if not path:
                logger.info(
                    "go_to_no_path", pos=f"({sx},{sy},{ss.z})",
                    target=f"({target_x},{target_y})", recalc=recalcs,
                )
                return False
            logger.debug(
                "go_to_path_found", pos=f"({sx},{sy})",
                path_len=len(path), recalc=recalcs,
            )

        # Wait until walker is ready
        for _ in range(20):
            if ctx.walker.can_walk():
                break
            await asyncio.sleep(0.1)
        else:
            return False

        # Take next step from path
        next_x, next_y = path[0]

        # If we're already past this waypoint (e.g. position changed), skip it
        if (next_x, next_y) == (sx, sy):
            path.pop(0)
            continue

        direction = direction_to(sx, sy, next_x, next_y)
        dir_name = _DIR_NAMES.get(direction, "?")

        # Remember position before step to detect success/failure
        prev_x, prev_y = sx, sy

        ctx.walker._pending_step_tile = (next_x, next_y)
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        await ctx.conn.send_packet(build_walk_request(direction, seq, fastwalk))
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
        )
        steps_taken += 1

        logger.debug(
            "go_to_step",
            pos=f"({sx},{sy})", next=f"({next_x},{next_y})",
            dir=dir_name, seq=seq, step=steps_taken, remaining=remaining,
        )

        # Wait for server response
        await asyncio.sleep(step_delay + 0.05)

        # Check if we moved
        if ss.x != prev_x or ss.y != prev_y:
            path.pop(0)
        else:
            logger.info(
                "go_to_denied",
                pos=f"({sx},{sy})", blocked=f"({next_x},{next_y})",
                dir=dir_name, recalc=recalcs,
            )
            path = []  # force recalculation on next iteration

    logger.info(
        "go_to_max_steps", pos=f"({ss.x},{ss.y})",
        target=f"({target_x},{target_y})", steps=steps_taken,
    )
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

    # Reset escape counter only when position actually changed (not just
    # after cooldown from a failed escape — that was resetting the counter
    # before full_clear ever triggered, causing an infinite stuck loop).
    last_pos = ctx.blackboard.get("_last_wander_pos")
    current_pos = (sx, sy)
    if last_pos is not None and last_pos != current_pos:
        ctx.blackboard.pop("escape_fail_count", None)
    ctx.blackboard["_last_wander_pos"] = current_pos

    # If too many consecutive denials, try to escape instead of just cooling down
    if ctx.walker.consecutive_denials >= 5:
        logger.info("wander_stuck", denials=ctx.walker.consecutive_denials)
        escape_fails = ctx.blackboard.get("escape_fail_count", 0)
        if ctx.map_reader is not None:
            escaped = await _escape_stuck(ctx, full_clear=escape_fails >= 2)
            ctx.blackboard["escape_fail_count"] = escape_fails + 1
            if escaped:
                ctx.walker.consecutive_denials = 0
                return Status.SUCCESS
        # Couldn't escape — cooldown, but keep denials high so we
        # re-enter escape quickly (1 more denial) instead of wasting 5 walks.
        ctx.walker.consecutive_denials = 4
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
    current_dir = ctx.perception.self_state.direction

    dir_name = _DIR_NAMES.get(direction, "?")

    # UO: if direction differs, first walk packet turns only (no move).
    # Send turn immediately, then send the actual step right after.
    if current_dir != direction:
        old_dir_name = _DIR_NAMES.get(current_dir, "?")
        logger.debug(
            "wander_turn",
            pos=f"({sx},{sy})", from_dir=old_dir_name, to_dir=dir_name,
        )
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.perception.self_state.direction = direction
        # Brief pause for server to process the turn
        await asyncio.sleep(0.1)

    # Send actual move step
    logger.debug(
        "wander_step",
        pos=f"({sx},{sy})", next=f"({nx},{ny})", dir=dir_name,
    )
    ctx.walker._pending_step_tile = (nx, ny)
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
    """Collect (x, y) of ground-level world items that actually have the IMPASSABLE flag.

    Previously this blocked ALL ground items, which incorrectly treated walkable
    items (logs, ore, etc.) as obstacles — trapping the agent after gathering.
    Items that block without the flag are handled by the denied_tiles cache.
    """
    if ctx.map_reader is None:
        return set()
    from anima.map import FLAG_IMPASSABLE

    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.serial & 0x40000000 == 0:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if flags & FLAG_IMPASSABLE:
            blocked.add((it.x, it.y))
    return blocked


async def _escape_stuck(ctx: BrainContext, full_clear: bool = False) -> bool:
    """Try to pathfind to an open tile when all 8 adjacent tiles are blocked.

    Searches outward in a spiral pattern for a walkable tile, then
    uses pathfinding (with larger max_steps) to get there.

    Clears nearby denied tiles first — they may have been blocked by
    dynamic obstacles (NPCs/mobiles) that have since moved.

    If full_clear=True, clears ALL denied tiles (used after repeated escape failures).
    """
    ss = ctx.perception.self_state
    sx, sy, sz = ss.x, ss.y, ss.z

    if full_clear:
        cleared = len(ctx.walker.denied_tiles)
        ctx.walker.clear_all_denied_tiles()
        logger.info("escape_full_clear", cleared=cleared)
    else:
        # Clear denied tiles within radius 12 so the pathfinder can try them again.
        # If they're still blocked, the server will deny and re-add them.
        cleared = 0
        for dy in range(-12, 13):
            for dx in range(-12, 13):
                tile_key = (sx + dx, sy + dy)
                if tile_key in ctx.walker.denied_tiles:
                    del ctx.walker.denied_tiles[tile_key]
                    cleared += 1
        if cleared:
            logger.info("escape_clear_denied", cleared=cleared, radius=12)

    denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)

    # Search for an open tile in expanding radius
    escape_path = None
    for radius in range(2, 25):
        if escape_path:
            break
        for dy in range(-radius, radius + 1):
            if escape_path:
                break
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
                    escape_path = (tx, ty, path, radius)
                    break

    if escape_path:
        tx, ty, path, radius = escape_path
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
        # Take the first step and verify it succeeded
        next_x, next_y = path[0]
        direction = direction_to(sx, sy, next_x, next_y)
        ctx.walker._pending_step_tile = (next_x, next_y)
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000
            + ctx.cfg.movement.walk_delay_ms
        )
        # Wait for server response and verify position actually changed
        await asyncio.sleep(0.5)
        if ss.x != sx or ss.y != sy:
            ctx.blackboard["move_target"] = (tx, ty)
            ctx.blackboard.pop("current_goal", None)
            return True
        # Step was denied despite map saying walkable — fall through to brute force
        logger.info("escape_path_denied", target=f"({tx},{ty})")

    # Last resort: brute-force walk in all 8 directions ignoring map data
    logger.warning("escape_brute_force", pos=f"({sx},{sy})")
    ctx.walker.clear_all_denied_tiles()
    for direction in range(8):
        if not ctx.walker.can_walk():
            await asyncio.sleep(0.5)
            if not ctx.walker.can_walk():
                continue
        dx, dy = DIRECTION_DELTAS[direction]
        nx, ny = sx + dx, sy + dy
        ctx.walker._pending_step_tile = (nx, ny)
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + 400
        )
        await asyncio.sleep(0.5)
        if ss.x != sx or ss.y != sy:
            logger.info(
                "escape_brute_success",
                direction=direction,
                pos=f"({ss.x},{ss.y})",
            )
            return True

    return False
