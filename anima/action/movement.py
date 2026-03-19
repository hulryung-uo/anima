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

    Returns True if destination reached, False if path blocked or failed.
    """
    max_retries = 3
    for _ in range(max_retries):
        sx = ctx.perception.self_state.x
        sy = ctx.perception.self_state.y

        if sx == target_x and sy == target_y:
            return True

        denied = set(ctx.walker.denied_tiles.keys())
        path = find_path(ctx.map_reader, sx, sy, target_x, target_y, denied_tiles=denied)
        if not path:
            return False

        for wx, wy in path:
            if not ctx.conn.connected:
                return False

            cx = ctx.perception.self_state.x
            cy = ctx.perception.self_state.y
            direction = direction_to(cx, cy, wx, wy)

            # Wait until we can walk
            for _ in range(50):  # ~2.5s max wait
                if ctx.walker.can_walk():
                    break
                await asyncio.sleep(0.05)
            else:
                return False

            # Turn first if needed
            current_dir = ctx.perception.self_state.direction
            if current_dir != direction:
                seq = ctx.walker.next_sequence()
                fastwalk = ctx.walker.pop_fast_walk_key()
                pkt = build_walk_request(direction, seq, fastwalk)
                await ctx.conn.send_packet(pkt)
                ctx.walker.steps_count += 1
                ctx.walker.last_step_time = (
                    asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.turn_delay_ms
                )
                ctx.perception.self_state.direction = direction
                # Wait for turn delay
                await asyncio.sleep(ctx.cfg.movement.turn_delay_ms / 1000.0)

            # Wait until we can walk again
            for _ in range(50):
                if ctx.walker.can_walk():
                    break
                await asyncio.sleep(0.05)
            else:
                return False

            # Record pending step for denial tracking
            ctx.walker._pending_step_tile = (wx, wy)

            # Take the step
            seq = ctx.walker.next_sequence()
            fastwalk = ctx.walker.pop_fast_walk_key()
            pkt = build_walk_request(direction, seq, fastwalk)
            await ctx.conn.send_packet(pkt)
            ctx.walker.steps_count += 1
            ctx.walker.last_step_time = (
                asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
            )

            # Wait for walk delay
            await asyncio.sleep(ctx.cfg.movement.walk_delay_ms / 1000.0)

            # Check if walk was denied (position didn't change as expected)
            new_x = ctx.perception.self_state.x
            new_y = ctx.perception.self_state.y
            if new_x != wx or new_y != wy:
                # Walk denied — re-pathfind from corrected position
                logger.debug(
                    "go_to_repath",
                    from_pos=f"({new_x},{new_y})",
                    target=f"({target_x},{target_y})",
                )
                break
        else:
            # All steps completed without break
            return True

    return ctx.perception.self_state.x == target_x and ctx.perception.self_state.y == target_y


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

    # If too many consecutive denials even during wander, stop trying for a while
    if ctx.walker.consecutive_denials >= 5:
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + 5000
        )
        logger.info("wander_stuck_cooldown", denials=ctx.walker.consecutive_denials)
        ctx.walker.consecutive_denials = 0
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

    # Score each direction
    candidates: list[tuple[int, float]] = []

    for direction in range(8):
        dx, dy = DIRECTION_DELTAS[direction]
        nx, ny = sx + dx, sy + dy

        if ctx.map_reader is not None:
            tile = ctx.map_reader.get_tile(nx, ny)
            if not tile.walkable:
                continue

        if ctx.walker.is_tile_denied(nx, ny):
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

    # Record pending step for denial tracking (turns also send walk packets)
    ctx.walker._pending_step_tile = (nx, ny)

    # Turn first if needed
    current_dir = ctx.perception.self_state.direction
    if current_dir != direction:
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.turn_delay_ms
        )
        ctx.perception.self_state.direction = direction
        return Status.SUCCESS

    # Take a step
    seq = ctx.walker.next_sequence()
    fastwalk = ctx.walker.pop_fast_walk_key()
    pkt = build_walk_request(direction, seq, fastwalk)
    await ctx.conn.send_packet(pkt)
    ctx.walker.steps_count += 1
    ctx.walker.last_step_time = (
        asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
    )

    return Status.SUCCESS
