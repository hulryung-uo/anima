"""High-level movement actions: go_to, wander."""

from __future__ import annotations

import asyncio
import random
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

        path = find_path(ctx.map_reader, sx, sy, target_x, target_y)
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
    """Pick a random nearby walkable tile and take one step toward it.

    Returns SUCCESS after taking a step, FAILURE if stuck.
    """
    from anima.brain.behavior_tree import Status

    sx = ctx.perception.self_state.x
    sy = ctx.perception.self_state.y

    if not ctx.walker.can_walk():
        return Status.RUNNING

    # Pick a random direction and check walkability
    directions = list(range(8))
    random.shuffle(directions)

    for direction in directions:
        dx, dy = DIRECTION_DELTAS[direction]
        nx, ny = sx + dx, sy + dy

        if ctx.map_reader is None:
            # No map reader — just walk in a random direction
            break

        tile = ctx.map_reader.get_tile(nx, ny)
        if tile.walkable:
            break
    else:
        return Status.FAILURE

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
