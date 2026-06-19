"""High-level movement actions: go_to, wander."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
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

# Auto-run policy: run on long legs with enough stamina, walk for
# precision near the target. The 0x80 run bit rides on the direction
# byte of the 0x02 walk packet; server cadence for unmounted run is
# run_delay_ms (200) vs walk_delay_ms (400).
RUN_FLAG = 0x80
RUN_MIN_REMAINING = 8     # tiles left — below this, walk for precision
RUN_MIN_STAM_PCT = 30.0   # don't run toward the stam==0 walk lockout


def _arrived(sx: int, sy: int, tx: int, ty: int, exact: bool) -> bool:
    """Arrival predicate for go_to.

    exact=True  -> the avatar must stand on the exact target tile.
    exact=False -> within 1 Chebyshev tile (adjacent) counts as arrived.

    Used by BOTH the per-step check and the max_steps fallback so the two
    never disagree (the fallback used to report success at 1 tile out even
    when exact=True was requested).
    """
    return max(abs(tx - sx), abs(ty - sy)) <= (0 if exact else 1)


def _should_run(ss, remaining: int, cfg, override: bool | None) -> bool:
    """Decide walk vs run for the next step of go_to."""
    if override is not None:
        return override
    if not getattr(cfg.movement, "run_enabled", True):
        return False
    if remaining < RUN_MIN_REMAINING:
        return False
    if ss.stam_max <= 0:  # stamina unknown — be conservative
        return False
    return (ss.stam / ss.stam_max) * 100.0 >= RUN_MIN_STAM_PCT


async def go_to(
    ctx: BrainContext,
    target_x: int,
    target_y: int,
    interrupt_check: Callable[[], bool] | None = None,
    exact: bool = False,
    run: bool | None = None,
) -> bool:
    """Pathfind and walk step-by-step to (target_x, target_y).

    Returns True if destination reached (within 1 tile, or exact tile if exact=True), False if failed.

    Features:
    - Recalculates path around permanently denied tiles
    - Opens closed doors automatically when blocked
    - Interrupt callback for survival checks between steps
    - Scales max_steps with distance (long-distance travel supported)
    - Auto-runs on long legs (run=None); run=True/False forces the mode
    """
    ss = ctx.perception.self_state
    step_delay = ctx.cfg.movement.walk_delay_ms / 1000.0

    dist = max(abs(target_x - ss.x), abs(target_y - ss.y))
    max_steps = max(200, dist * 3)  # scale with distance, minimum 200
    max_recalc = 20  # enough to route around a full building wall

    logger.info(
        "go_to_start",
        pos=f"({ss.x},{ss.y},{ss.z})",
        target=f"({target_x},{target_y})",
        dist=dist, max_steps=max_steps,
    )

    import time as _time
    _start_time = _time.monotonic()
    steps_taken = 0
    recalcs = 0
    doors_opened = 0
    path: list[tuple[int, int]] = []
    permanent_denied: set[tuple[int, int]] = set()  # tiles that always block
    _door_tried: set[tuple[int, int]] = set()  # door tiles already attempted
    _opened_door_positions: set[tuple[int, int]] = set()  # positions where opened doors moved TO
    _mobile_waits: dict[tuple[int, int], int] = {}  # tiles waited on due to NPC blocking
    consecutive_denials_without_progress = 0
    consecutive_turn_failures = 0  # server kept rejecting our turn (e.g. Frozen)
    last_progress_pos: tuple[int, int] = (ss.x, ss.y)

    while steps_taken < max_steps and ctx.conn.connected:
        # Check for interrupt between each step (e.g., HP low → flee)
        if interrupt_check is not None and interrupt_check():
            logger.info("go_to_interrupted", pos=f"({ss.x},{ss.y})", steps=steps_taken)
            return False

        sx, sy = ss.x, ss.y
        remaining = max(abs(target_x - sx), abs(target_y - sy))
        if _arrived(sx, sy, target_x, target_y, exact):
            elapsed = _time.monotonic() - _start_time
            logger.info(
                "go_to_arrived",
                pos=f"({sx},{sy})", steps=steps_taken,
                time=f"{elapsed:.1f}s", recalcs=recalcs, doors=doors_opened,
            )
            return True

        # Calculate path if we don't have one
        if not path:
            if recalcs >= max_recalc:
                logger.info(
                    "go_to_give_up", pos=f"({sx},{sy})",
                    target=f"({target_x},{target_y})", recalcs=recalcs,
                    permanent_denied=len(permanent_denied),
                )
                return False

            # Combine denied tiles: walker denials + permanent (from server denies)
            walker_denied = set(ctx.walker.denied_tiles.keys())
            denied = walker_denied | permanent_denied

            # Collect dynamic door world items → force passable in pathfinder
            door_positions = _get_door_positions(ctx)

            search_steps = max(10000, dist * 30)  # urban terrain needs larger budget
            adj = not exact
            path = find_path(
                ctx.map_reader, sx, sy, target_x, target_y,
                max_steps=search_steps,
                denied_tiles=denied, current_z=ss.z,
                adjacent=adj,
                door_tiles=door_positions,
            )
            recalcs += 1

            if not path:
                # Try without walker denied (might be stale), keep permanent
                path = find_path(
                    ctx.map_reader, sx, sy, target_x, target_y,
                    max_steps=search_steps,
                    denied_tiles=permanent_denied,
                    door_tiles=door_positions,
                    current_z=ss.z,
                    adjacent=adj,
                )
                if path:
                    logger.info("go_to_clear_walker_denied", cleared=len(walker_denied))
                    ctx.walker.clear_all_denied_tiles()
                    continue

                # Try alternate z levels (cave z=0 vs surface z=15)
                for alt_z in (0, 15, 25, 35):
                    if alt_z == ss.z:
                        continue
                    path = find_path(
                        ctx.map_reader, sx, sy, target_x, target_y,
                        max_steps=search_steps,
                        denied_tiles=permanent_denied,
                        door_tiles=door_positions,
                        current_z=alt_z,
                        adjacent=adj,
                    )
                    if path:
                        logger.info("go_to_alt_z", z=alt_z, path_len=len(path))
                        break

                if path:
                    continue

                # Intermediate-target fallback: if the full path is too
                # complex for A*, try reaching a point ~15 tiles toward
                # the destination.  The main loop will re-pathfind to
                # the original target from the new position.
                if remaining > 15:
                    frac = 15.0 / remaining
                    mid_x = sx + int((target_x - sx) * frac)
                    mid_y = sy + int((target_y - sy) * frac)
                    mid_path = find_path(
                        ctx.map_reader, sx, sy, mid_x, mid_y,
                        max_steps=search_steps,
                        denied_tiles=permanent_denied,
                        current_z=ss.z,
                        adjacent=True,
                        door_tiles=door_positions,
                    )
                    if mid_path:
                        logger.info(
                            "go_to_intermediate",
                            pos=f"({sx},{sy})",
                            mid=f"({mid_x},{mid_y})",
                            target=f"({target_x},{target_y})",
                            mid_len=len(mid_path),
                        )
                        path = mid_path
                        continue

                # Known-waypoint fallback: try nearby navigable locations
                # as intermediate targets.  These are outdoor positions
                # that are more likely reachable than arbitrary midpoints.
                # Phase 1: waypoints toward the target (progress + escape).
                # Phase 2: any nearby waypoint (pure escape — changing
                # position may unblock re-pathfinding from the new spot).
                if remaining > 10:
                    try:
                        from anima.world_knowledge import nearest_locations
                        wp_found = False
                        waypoints = nearest_locations(sx, sy, count=10)
                        for phase in (1, 2):
                            if wp_found:
                                break
                            for loc, loc_dist in waypoints:
                                if loc_dist < 5 or loc_dist > 60:
                                    continue
                                if phase == 1:
                                    loc_to_target = max(
                                        abs(target_x - loc.x),
                                        abs(target_y - loc.y),
                                    )
                                    if loc_to_target >= remaining:
                                        continue
                                wp_path = find_path(
                                    ctx.map_reader, sx, sy,
                                    loc.nav_x, loc.nav_y,
                                    max_steps=search_steps,
                                    denied_tiles=permanent_denied,
                                    current_z=ss.z,
                                    adjacent=True,
                                    door_tiles=door_positions,
                                )
                                if wp_path:
                                    logger.info(
                                        "go_to_waypoint_fallback",
                                        waypoint=loc.name,
                                        pos=f"({sx},{sy})",
                                        via=f"({loc.nav_x},{loc.nav_y})",
                                        target=f"({target_x},{target_y})",
                                        wp_len=len(wp_path),
                                        phase=phase,
                                    )
                                    path = wp_path
                                    wp_found = True
                                    break
                        if wp_found:
                            continue
                    except ImportError:
                        pass

                logger.info(
                    "go_to_no_path", pos=f"({sx},{sy},{ss.z})",
                    denied=len(denied),
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
            # Walker stuck — log state and force-reset so the next go_to
            # attempt has a chance.  The server-side walk state is already
            # out of sync if we reach here, so resetting is the safest
            # recovery.
            logger.warning(
                "go_to_walker_stuck",
                steps_count=ctx.walker.steps_count,
                walk_seq=ctx.walker.walk_sequence,
                last_step_time=ctx.walker.last_step_time,
                walking_failed=ctx.walker.walking_failed,
                pos=f"({ss.x},{ss.y})",
                target=f"({target_x},{target_y})",
            )
            ctx.walker.steps_count = 0
            ctx.walker.walk_sequence = 0
            ctx.walker.walking_failed = False
            return False

        # Take next step from path
        next_x, next_y = path[0]

        # Skip waypoints we've already passed
        if (next_x, next_y) == (sx, sy):
            path.pop(0)
            continue

        # --- DOOR CHECK: if next tile has a closed door, run traversal routine ---
        door_serial = _find_closed_door_at(ctx, next_x, next_y, _opened_door_positions)
        if door_serial is not None and (next_x, next_y) not in _door_tried:
            _door_tried.add((next_x, next_y))
            ok, opened_new_pos = await _traverse_door(ctx, door_serial, sx, sy, next_x, next_y, step_delay)
            if opened_new_pos:
                _opened_door_positions.add(opened_new_pos)
                doors_opened += 1
                permanent_denied.add(opened_new_pos)  # opened door graphic is impassable
            if ok:
                path = []
            else:
                permanent_denied.add((next_x, next_y))
                path = []
            continue

        direction = direction_to(sx, sy, next_x, next_y)
        dir_name = _DIR_NAMES.get(direction, "?")
        prev_x, prev_y = sx, sy

        current_facing = ss.direction & 0x07
        need_turn = (current_facing != direction)

        if need_turn:
            # Turn packet — _pending_step_tile MUST be None
            ctx.walker._pending_step_tile = None
            ctx.walker._pending_direction = direction
            seq = ctx.walker.next_sequence()
            fastwalk = ctx.walker.pop_fast_walk_key()
            await ctx.conn.send_packet(build_walk_request(direction, seq, fastwalk))
            ctx.walker.steps_count += 1
            ctx.walker.mark_turn()
            # Wait for turn confirm before sending move
            await asyncio.sleep(step_delay + 0.05)
            await asyncio.sleep(0)  # yield to process confirm

            # Detect persistent turn rejection (e.g. server Frozen=true).
            # Turn packets don't set _pending_step_tile, so denials don't
            # mark any tile and steps_taken never advances — without this
            # check the outer while-loop runs forever.
            if (ss.direction & 0x07) == direction:
                consecutive_turn_failures = 0
            else:
                consecutive_turn_failures += 1
                if consecutive_turn_failures >= 5:
                    logger.info(
                        "go_to_turn_stuck",
                        pos=f"({ss.x},{ss.y})",
                        target_dir=dir_name,
                        current_dir=_DIR_NAMES.get(ss.direction & 0x07, "?"),
                        failures=consecutive_turn_failures,
                    )
                    return False
            # Don't count turn as a step
            continue

        # Move packet — set _pending_step_tile for position tracking
        use_run = _should_run(ss, remaining, ctx.cfg, run)
        ctx.walker._pending_step_tile = (next_x, next_y)
        move_seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        await ctx.conn.send_packet(build_walk_request(
            direction | (RUN_FLAG if use_run else 0), move_seq, fastwalk,
        ))
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + (
                ctx.cfg.movement.run_delay_ms if use_run
                else ctx.cfg.movement.walk_delay_ms
            )
        )
        steps_taken += 1

        logger.debug(
            "go_to_step",
            pos=f"({sx},{sy})", next=f"({next_x},{next_y})",
            dir=dir_name, seq=move_seq, step=steps_taken, remaining=remaining,
        )

        # Wait for server to process — poll until position changes or timeout
        deadline = asyncio.get_event_loop().time() + 1.0  # 1 second max
        polls = 0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            polls += 1
            if ss.x != prev_x or ss.y != prev_y:
                logger.debug("go_to_confirmed", polls=polls, pos=f"({ss.x},{ss.y})")
                break

        if (ss.x, ss.y) == (next_x, next_y):
            path.pop(0)
        elif ss.x != prev_x or ss.y != prev_y:
            # Position changed but NOT to the requested tile — a server
            # resync/teleport (0x20) landed mid-walk. The rest of the path
            # is stale; popping blind walks the old plan from the wrong
            # origin. Drop it and re-pathfind from where we actually are.
            logger.info(
                "go_to_resync",
                pos=f"({ss.x},{ss.y})",
                expected=f"({next_x},{next_y})",
            )
            path = []
            continue
        else:
            # Walk denied — check if it's stamina depletion, not terrain.
            # UO server rejects ALL walks at stam==0 ("too fatigued").
            if ss.stam_max > 0 and ss.stam < 2:
                logger.info(
                    "go_to_fatigue_wait",
                    stam=ss.stam,
                    stam_max=ss.stam_max,
                    pos=f"({sx},{sy})",
                )
                for _ in range(30):
                    if interrupt_check is not None and interrupt_check():
                        return False
                    await asyncio.sleep(1.0)
                    if ss.stam >= 2:
                        break
                if ss.stam >= 2:
                    path = []
                    continue
                logger.info("go_to_fatigue_timeout", stam=ss.stam, pos=f"({sx},{sy})")
                return False

            # Check if a mobile (NPC/animal) is on the denied tile.
            # NPCs wander and vacate tiles — don't permanently deny them.
            # Wait briefly for the NPC to move, then retry the path.
            mobile_at_tile = any(
                mob.x == next_x and mob.y == next_y
                for mob in ctx.perception.world.mobiles.values()
                if mob.serial != ss.serial
            )
            if mobile_at_tile:
                waits = _mobile_waits.get((next_x, next_y), 0)
                if waits < 3:
                    _mobile_waits[(next_x, next_y)] = waits + 1
                    logger.info(
                        "go_to_mobile_blocked",
                        pos=f"({sx},{sy})", blocked=f"({next_x},{next_y})",
                        wait=waits + 1,
                    )
                    await asyncio.sleep(1.5)
                    path = []  # recalculate — NPC may have moved
                    continue

            # Not a door, not a mobile — add to permanent denied.
            # Only deny the exact tile the server rejected.
            # Speculative probing of perpendicular tiles was removed because
            # it creates false denials at building corners, blocking the
            # only valid route around the wall.
            permanent_denied.add((next_x, next_y))

            # Track if we're making progress or stuck in the same spot
            if abs(sx - last_progress_pos[0]) <= 2 and abs(sy - last_progress_pos[1]) <= 2:
                consecutive_denials_without_progress += 1
            else:
                consecutive_denials_without_progress = 0
                last_progress_pos = (sx, sy)

            logger.info(
                "go_to_denied",
                pos=f"({sx},{sy})", blocked=f"({next_x},{next_y})",
                dir=dir_name, recalc=recalcs,
                permanent_denied=len(permanent_denied),
                stuck_count=consecutive_denials_without_progress,
            )

            # If stuck in same area for 3+ denials, try escaping:
            # 1. Try opening ALL nearby doors (might be trapped in a building)
            # 2. Walk perpendicular to the blocked direction
            if consecutive_denials_without_progress >= 3:
                # Try to open any nearby doors first
                doors_opened = await _try_open_nearby_doors(ctx)
                if doors_opened > 0:
                    logger.info("go_to_opened_nearby_doors", count=doors_opened)
                    # Clear walker denials since doors changed walkability
                    ctx.walker.clear_all_denied_tiles()
                    consecutive_denials_without_progress = 0
                    path = []  # recalculate with doors open
                    continue

                # No doors found — try a detour
                detour_targets = _calc_detour(sx, sy, target_x, target_y)
                for dx2, dy2 in detour_targets:
                    logger.info("go_to_detour", target=f"({dx2},{dy2})")
                    detour_path = find_path(
                        ctx.map_reader, sx, sy, dx2, dy2,
                        max_steps=100,
                        denied_tiles=_impassable_world_items(ctx) | permanent_denied,
                        current_z=ss.z,
                        adjacent=True,
                    )
                    if detour_path:
                        path = detour_path
                        consecutive_denials_without_progress = 0
                        break
                if path:
                    continue

            path = []  # force recalculation with expanded denied set

    logger.info(
        "go_to_max_steps", pos=f"({ss.x},{ss.y})",
        target=f"({target_x},{target_y})", steps=steps_taken,
    )
    return _arrived(ss.x, ss.y, target_x, target_y, exact)


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

    for direction in range(8):  # All 8 directions including diagonals
        dx, dy = DIRECTION_DELTAS[direction]
        nx, ny = sx + dx, sy + dy

        if ctx.map_reader is not None:
            tile = ctx.map_reader.get_tile(nx, ny)
            can_walk, _ = tile.walkable_z(sz)
            if not can_walk:
                continue
            # Diagonal corner-cutting check
            if dx != 0 and dy != 0:
                t1 = ctx.map_reader.get_tile(sx + dx, sy)
                t2 = ctx.map_reader.get_tile(sx, sy + dy)
                ok1, _ = t1.walkable_z(sz)
                ok2, _ = t2.walkable_z(sz)
                if not (ok1 and ok2):
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
        # Turn packet carries NO position — clear any stale pending move tile
        # before stamping the turn's sequence, and route the facing through
        # _pending_direction so it's applied on the turn's own confirm. Without
        # the reset, a leftover _pending_step_tile from a prior un-acked move
        # rides on the turn's seq; the turn's ConfirmWalk (which matches that
        # seq) then snaps the avatar onto that stale tile — a desync. go_to and
        # _walk_one_step already guard this; wander_action must too.
        ctx.walker._pending_step_tile = None
        ctx.walker._pending_direction = direction
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


async def _walk_one_step(
    ctx: BrainContext, direction: int, step_delay: float,
) -> bool:
    """Walk one tile in the given direction. Handles turn-then-move.

    Returns True if position changed.
    """
    ss = ctx.perception.self_state
    prev_x, prev_y = ss.x, ss.y

    # Wait until walker is ready
    for _ in range(20):
        if ctx.walker.can_walk():
            break
        await asyncio.sleep(0.1)
    else:
        logger.warning(
            "walk_one_step_walker_stuck",
            steps_count=ctx.walker.steps_count,
            walk_seq=ctx.walker.walk_sequence,
        )
        ctx.walker.steps_count = 0
        ctx.walker.walk_sequence = 0
        ctx.walker.walking_failed = False
        return False

    # Turn first if not facing the right direction
    current_facing = ss.direction & 0x07
    if current_facing != direction:
        ctx.walker._pending_step_tile = None  # turn only — no position update
        ctx.walker._pending_direction = direction
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        await ctx.conn.send_packet(build_walk_request(direction, seq, fastwalk))
        ctx.walker.steps_count += 1
        ctx.walker.mark_turn()
        await asyncio.sleep(0.05)
        for _ in range(5):
            if ctx.walker.can_walk():
                break
            await asyncio.sleep(0.02)

    # Now move — set _pending_step_tile for position tracking
    ctx.walker._pending_step_tile = (
        ss.x + DIRECTION_DELTAS[direction][0],
        ss.y + DIRECTION_DELTAS[direction][1],
    )
    seq = ctx.walker.next_sequence()
    fastwalk = ctx.walker.pop_fast_walk_key()
    await ctx.conn.send_packet(build_walk_request(direction, seq, fastwalk))
    ctx.walker.steps_count += 1
    ctx.walker.last_step_time = (
        asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
    )
    await asyncio.sleep(step_delay + 0.05)

    return ss.x != prev_x or ss.y != prev_y


def _cardinal_direction(from_x: int, from_y: int, to_x: int, to_y: int) -> int:
    """Get the nearest cardinal direction (N/E/S/W only, no diagonals)."""
    dx = to_x - from_x
    dy = to_y - from_y
    if abs(dx) >= abs(dy):
        return 2 if dx > 0 else 6  # East or West
    else:
        return 4 if dy > 0 else 0  # South or North


async def _traverse_door(  # noqa: C901
    ctx: BrainContext,
    door_serial: int,
    player_x: int,
    player_y: int,
    door_x: int,
    door_y: int,
    step_delay: float,
) -> tuple[bool, tuple[int, int] | None]:
    """Full door traversal routine:

    1. Approach door from cardinal direction (N/E/S/W)
    2. Open door (double-click)
    3. Verify it opened (position changed)
    4. Walk 2 tiles through (door tile + 1 past)
    5. Close door (double-click from other side)

    Returns (success, opened_door_new_position).
    """
    from anima.client.packets import build_double_click

    ss = ctx.perception.self_state

    # Determine approach direction (cardinal only)
    walk_dir = _cardinal_direction(player_x, player_y, door_x, door_y)
    dx, dy = DIRECTION_DELTAS[walk_dir]
    approach_x = door_x - dx
    approach_y = door_y - dy

    dir_name = _DIR_NAMES.get(walk_dir, "?")
    logger.info(
        "door_traverse_start",
        door=f"({door_x},{door_y})",
        approach=f"({approach_x},{approach_y})",
        direction=dir_name,
    )

    # Step 1: Walk to approach tile (adjacent to door, cardinal direction)
    if ss.x != approach_x or ss.y != approach_y:
        # Simple direct walk to approach tile
        for _ in range(5):
            if max(abs(ss.x - approach_x), abs(ss.y - approach_y)) == 0:
                break
            d = direction_to(ss.x, ss.y, approach_x, approach_y)
            if not await _walk_one_step(ctx, d, step_delay):
                break

    dx_to_door = abs(ss.x - door_x)
    dy_to_door = abs(ss.y - door_y)
    # Must be exactly 1 tile away in a cardinal direction (not diagonal)
    if not ((dx_to_door == 1 and dy_to_door == 0) or (dx_to_door == 0 and dy_to_door == 1)):
        logger.info("door_not_adjacent_cardinal", pos=f"({ss.x},{ss.y})", door=f"({door_x},{door_y})")
        return False, None

    # Step 2: Open the door
    door_item = ctx.perception.world.items.get(door_serial)
    if not door_item:
        return False, None
    old_door_pos = (door_item.x, door_item.y)

    await ctx.conn.send_packet(build_double_click(door_serial))
    await asyncio.sleep(1.0)

    # Step 3: Verify door opened (position changed)
    door_item = ctx.perception.world.items.get(door_serial)
    if not door_item:
        return False, None
    new_door_pos = (door_item.x, door_item.y)

    if new_door_pos == old_door_pos:
        logger.info("door_didnt_open", pos=f"{old_door_pos}")
        return False, None

    logger.info("door_opened", old=f"{old_door_pos}", new=f"{new_door_pos}")

    # Step 4: Walk 2 tiles through the door (through old door pos + 1 more)
    for step in range(2):
        moved = await _walk_one_step(ctx, walk_dir, step_delay)
        if not moved:
            logger.info("door_walk_through_failed", step=step + 1)
            break

    # Step 5: Close the door (double-click from other side)
    door_item = ctx.perception.world.items.get(door_serial)
    if door_item and max(abs(door_item.x - ss.x), abs(door_item.y - ss.y)) <= 2:
        old_close_pos = (door_item.x, door_item.y)
        await ctx.conn.send_packet(build_double_click(door_serial))
        await asyncio.sleep(1.0)

        door_item = ctx.perception.world.items.get(door_serial)
        if door_item:
            new_close_pos = (door_item.x, door_item.y)
            if new_close_pos != old_close_pos:
                logger.info("door_closed", old=f"{old_close_pos}", new=f"{new_close_pos}")
            else:
                logger.debug("door_close_unchanged")

    logger.info(
        "door_traverse_done",
        pos=f"({ss.x},{ss.y})",
        started=f"({player_x},{player_y})",
        opened_door_at=f"{new_door_pos}",
    )
    return True, new_door_pos




async def _try_open_nearby_doors(ctx: BrainContext) -> int:
    """Find and open closed doors within 3 tiles. Returns number opened.

    Detects successful opening by checking if the door item moved position
    (UO doors shift position when opened).
    """
    if ctx.map_reader is None:
        return 0
    from anima.client.packets import build_double_click
    from anima.map import FLAG_DOOR, FLAG_IMPASSABLE

    ss = ctx.perception.self_state
    opened = 0
    for it in list(ctx.perception.world.items.values()):
        if it.container != 0:
            continue
        dist = max(abs(it.x - ss.x), abs(it.y - ss.y))
        if dist > 3:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if (flags & FLAG_DOOR) and (flags & FLAG_IMPASSABLE):
            old_pos = (it.x, it.y)
            await ctx.conn.send_packet(build_double_click(it.serial))
            await asyncio.sleep(1.0)
            # Check if door moved
            updated = ctx.perception.world.items.get(it.serial)
            if updated and (updated.x, updated.y) != old_pos:
                logger.info("door_opened", old=f"{old_pos}", new=f"({updated.x},{updated.y})")
                # Old pos is now walkable; new pos has the open door (impassable)
                ctx.walker.clear_denied_tile(old_pos[0], old_pos[1])
                opened += 1
            else:
                logger.debug("door_no_change", pos=f"{old_pos}")
    return opened


def _calc_detour(
    sx: int, sy: int,
    target_x: int, target_y: int,
) -> list[tuple[int, int]]:
    """Calculate detour points perpendicular to the blocked direction.

    When the agent is stuck hitting a wall, this returns points
    20 tiles perpendicular to the wall (both sides).
    The caller tries each until one yields a valid path.
    """
    # Direction from agent to target
    dx = target_x - sx
    dy = target_y - sy

    detour_dist = 20
    if abs(dx) >= abs(dy):
        # Primarily east/west movement — detour north and south
        return [(sx, sy - detour_dist), (sx, sy + detour_dist)]
    else:
        # Primarily north/south — detour east and west
        return [(sx + detour_dist, sy), (sx - detour_dist, sy)]


def _get_door_positions(ctx: BrainContext) -> set[tuple[int, int]]:
    """Collect positions of door world items (dynamic) for pathfinder."""
    if ctx.map_reader is None:
        return set()
    from anima.map import FLAG_DOOR

    doors: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if flags & FLAG_DOOR:
            doors.add((it.x, it.y))
    return doors


def _impassable_world_items(ctx: BrainContext) -> set[tuple[int, int]]:
    """Collect (x, y) of ground-level world items that have IMPASSABLE flag.

    Excludes doors — those are opened automatically by go_to().
    """
    if ctx.map_reader is None:
        return set()
    from anima.map import FLAG_DOOR, FLAG_IMPASSABLE

    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.serial & 0x40000000 == 0:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if (flags & FLAG_IMPASSABLE) and not (flags & FLAG_DOOR):
            blocked.add((it.x, it.y))
    return blocked


def _find_closed_door_at(
    ctx: BrainContext,
    x: int,
    y: int,
    exclude_positions: set[tuple[int, int]] | None = None,
) -> int | None:
    """Find a closed door world item AT exactly (x, y).

    Returns serial or None. Only returns doors that are impassable (closed).
    Skips positions in exclude_positions (already-opened doors).

    Exact match (not adjacency): door logic must only fire when the path
    actually steps onto the door tile. A loose adjacency check causes the
    agent to try opening a door from a non-cardinal position when the door
    merely neighbors the path — the server silently ignores the use, and
    the agent loops indefinitely.
    """
    if ctx.map_reader is None:
        return None
    from anima.map import FLAG_DOOR, FLAG_IMPASSABLE

    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.x == x and it.y == y:
            # Skip positions where we know an opened door moved to
            if exclude_positions and (it.x, it.y) in exclude_positions:
                continue
            flags = ctx.map_reader._get_item_flags(it.graphic)
            if (flags & FLAG_DOOR) and (flags & FLAG_IMPASSABLE):
                return it.serial
    return None


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
    for direction in (0, 2, 4, 6):  # N, E, S, W only
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
