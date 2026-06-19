"""Pathfinding algorithms for UO map grid.

Available algorithms:
- A* (default): optimal path, moderate speed
- Weighted A*: faster, near-optimal (weight=1.5)
- Greedy Best-First: fastest, may not be optimal

find_path() automatically tries weighted A* first, falls back to
standard A* with more steps if no path found.
"""

from __future__ import annotations

import heapq
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.map import MapReader

# UO direction constants: N=0, NE=1, E=2, SE=3, S=4, SW=5, W=6, NW=7
DIRECTION_DELTAS: dict[int, tuple[int, int]] = {
    0: (0, -1),  # North
    1: (1, -1),  # NorthEast
    2: (1, 0),   # East
    3: (1, 1),   # SouthEast
    4: (0, 1),   # South
    5: (-1, 1),  # SouthWest
    6: (-1, 0),  # West
    7: (-1, -1), # NorthWest
}

_DELTA_TO_DIR: dict[tuple[int, int], int] = {v: k for k, v in DIRECTION_DELTAS.items()}

SQRT2 = math.sqrt(2)


def direction_to(fx: int, fy: int, tx: int, ty: int) -> int:
    """Return the UO direction (0-7) from (fx,fy) to (tx,ty)."""
    dx = tx - fx
    dy = ty - fy
    if dx == 0 and dy == 0:
        return 0
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    return _DELTA_TO_DIR.get((sx, sy), 0)


def _octile_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    # True octile distance — admissible AND consistent for 8-directional
    # movement with cardinal cost=1 and diagonal cost=SQRT2. A diagonal step is
    # a single UO walk packet (same throttle as a cardinal step), so it must be
    # cheaper than two cardinal steps; otherwise A* is indifferent between a
    # diagonal and an L-shaped detour and can return a path with MORE walk
    # steps than necessary. octile = (dx+dy) + (SQRT2-2)*min(dx,dy), i.e.
    # max(dx,dy) + (SQRT2-1)*min(dx,dy).
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    return (dx + dy) + (SQRT2 - 2.0) * min(dx, dy)


def _is_walkable(
    map_reader: "MapReader",
    x: int, y: int,
    denied_tiles: set[tuple[int, int]] | None,
    current_z: int | None,
    z_at: dict[tuple[int, int], int] | None,
    cx: int, cy: int,
    doors_passable: bool = False,
    door_tiles: set[tuple[int, int]] | None = None,
) -> tuple[bool, int]:
    """Check if tile (x,y) is walkable. Returns (can_walk, new_z).

    If doors_passable=True, tiles blocked only by door statics are treated
    as walkable (the agent can open them during movement).

    If door_tiles is provided, tiles in this set are forced walkable
    (dynamic door world items detected at runtime).
    """
    if denied_tiles and (x, y) in denied_tiles:
        return False, 0

    # Dynamic door world items — force walkable
    if door_tiles and (x, y) in door_tiles:
        node_z = current_z or 0
        if z_at is not None:
            node_z = z_at.get((cx, cy), node_z)
        return True, node_z

    tile = map_reader.get_tile(x, y)

    if current_z is not None and z_at is not None:
        node_z = z_at.get((cx, cy), current_z)
        can_walk, new_z = tile.walkable_z(node_z)
        if not can_walk and doors_passable:
            if _only_door_blocks(tile):
                return True, new_z if new_z != 0 else node_z
        return can_walk, new_z
    else:
        walkable = tile.walkable
        if not walkable and doors_passable:
            if _only_door_blocks(tile):
                return True, 0
        return walkable, 0


def _only_door_blocks(tile) -> bool:
    """Check if the tile is blocked only by door statics (can be opened)."""
    from anima.map import FLAG_DOOR, FLAG_IMPASSABLE, FLAG_SURFACE

    if tile.land.impassable:
        return False  # land itself is impassable — not a door issue

    for s in tile.statics:
        if s.impassable and not s.surface:
            # This static blocks the tile — is it a door?
            if not (s.flags & FLAG_DOOR):
                return False  # non-door blocker exists
    return True  # all blockers are doors


def _astar_core(
    map_reader: "MapReader",
    sx: int, sy: int,
    tx: int, ty: int,
    max_steps: int,
    denied_tiles: set[tuple[int, int]] | None,
    current_z: int | None,
    heuristic_weight: float = 1.0,
    adjacent: bool = False,
    doors_passable: bool = False,
    door_tiles: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Core A* implementation with configurable heuristic weight.

    weight=1.0: standard A* (optimal)
    weight>1.0: weighted A* (faster, near-optimal)
    weight=inf: greedy best-first (fastest, not optimal)

    If adjacent=True, the goal is any tile within 1 Chebyshev distance
    of (tx, ty) — useful when the target itself is impassable (e.g. forge, anvil).
    """
    if sx == tx and sy == ty:
        return []

    if adjacent and max(abs(sx - tx), abs(sy - ty)) <= 1:
        return []

    counter = 0
    open_set: list[tuple[float, int, int, int]] = []
    heapq.heappush(open_set, (_octile_distance(sx, sy, tx, ty) * heuristic_weight, counter, sx, sy))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], float] = {(sx, sy): 0.0}
    closed: set[tuple[int, int]] = set()

    z_at: dict[tuple[int, int], int] = {}
    if current_z is not None:
        z_at[(sx, sy)] = current_z

    while open_set:
        _, _, cx, cy = heapq.heappop(open_set)

        goal_reached = (
            max(abs(cx - tx), abs(cy - ty)) <= 1
            if adjacent
            else (cx == tx and cy == ty)
        )
        if goal_reached:
            path: list[tuple[int, int]] = []
            node = (cx, cy)
            while node in came_from:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return path

        if (cx, cy) in closed:
            continue
        closed.add((cx, cy))

        if len(closed) > max_steps:
            return []

        for direction in range(8):  # All 8 directions (ClassicUO reference)
            dx, dy = DIRECTION_DELTAS[direction]
            nx, ny = cx + dx, cy + dy

            if (nx, ny) in closed:
                continue

            # Diagonal corner-cutting check: both perpendicular tiles must
            # be walkable, otherwise the server will deny the diagonal move.
            #
            # The server's corner-cut rule is purely about *map* impassability
            # (statics/land), NOT about our transient ``denied_tiles`` cache.
            # That cache mostly holds tiles a wandering mobile happened to stand
            # on when it denied us; a mobile on a perpendicular corner does not
            # forbid a diagonal step. Feeding ``denied_tiles`` into the side
            # checks turns one denied corner into a phantom wall that rejects
            # *every* diagonal through it — often the only route around an
            # obstacle — so go_to() replans in a loop and the agent gets stuck.
            # Side checks therefore ignore ``denied_tiles``; the destination
            # check below still honors it.
            is_diagonal = (dx != 0 and dy != 0)
            if is_diagonal:
                side1_ok, _ = _is_walkable(
                    map_reader, cx + dx, cy, None,
                    current_z, z_at if current_z is not None else None, cx, cy,
                    doors_passable=doors_passable, door_tiles=door_tiles,
                )
                side2_ok, _ = _is_walkable(
                    map_reader, cx, cy + dy, None,
                    current_z, z_at if current_z is not None else None, cx, cy,
                    doors_passable=doors_passable, door_tiles=door_tiles,
                )
                if not (side1_ok and side2_ok):
                    continue

            can_walk, new_z = _is_walkable(
                map_reader, nx, ny, denied_tiles, current_z, z_at if current_z is not None else None, cx, cy,
                doors_passable=doors_passable, door_tiles=door_tiles,
            )
            if not can_walk:
                continue

            move_cost = SQRT2 if is_diagonal else 1.0
            tentative_g = g_score[(cx, cy)] + move_cost

            if tentative_g < g_score.get((nx, ny), float("inf")):
                came_from[(nx, ny)] = (cx, cy)
                g_score[(nx, ny)] = tentative_g
                if current_z is not None:
                    z_at[(nx, ny)] = new_z
                h = _octile_distance(nx, ny, tx, ty) * heuristic_weight
                f = tentative_g + h
                counter += 1
                heapq.heappush(open_set, (f, counter, nx, ny))

    return []


def path_is_traversable(
    map_reader: "MapReader",
    sx: int,
    sy: int,
    path: list[tuple[int, int]],
    denied_tiles: set[tuple[int, int]] | None = None,
    current_z: int | None = None,
    doors_passable: bool = True,
    door_tiles: set[tuple[int, int]] | None = None,
) -> bool:
    """Re-validate a previously computed ``path`` against the *current* map.

    A* runs once and its result is cached, but the world changes underneath
    it: a mob walks onto a tile, a static is revealed, or the walker records a
    DenyWalk that lands a tile in ``denied_tiles``. Stepping blindly into a
    now-blocked tile costs a server round-trip (DenyWalk) and a stall. This
    helper lets a caller cheaply decide whether to reuse the cached path or
    replan *before* sending the next walk packet.

    Walking from ``(sx, sy)`` it verifies, for every node in ``path``:
      * the node is reachable in a single 8-directional step from the
        previous node, and
      * the destination tile is walkable, and
      * for a diagonal step both perpendicular tiles are walkable (the same
        corner-cutting rule A* enforces, since the server denies a diagonal
        that clips an impassable corner).

    Returns ``True`` only if the whole path is still walkable, ``False`` as
    soon as any step is blocked (or the path is malformed / non-contiguous).
    An empty path is vacuously traversable.
    """
    if not path:
        return True

    z_at: dict[tuple[int, int], int] = {}
    if current_z is not None:
        z_at[(sx, sy)] = current_z

    cx, cy = sx, sy
    for nx, ny in path:
        dx, dy = nx - cx, ny - cy
        # Each hop must be a single 8-directional step.
        if dx == 0 and dy == 0:
            return False
        if abs(dx) > 1 or abs(dy) > 1:
            return False

        z_arg = z_at if current_z is not None else None

        is_diagonal = (dx != 0 and dy != 0)
        if is_diagonal:
            side1_ok, _ = _is_walkable(
                map_reader, cx + dx, cy, denied_tiles,
                current_z, z_arg, cx, cy,
                doors_passable=doors_passable, door_tiles=door_tiles,
            )
            side2_ok, _ = _is_walkable(
                map_reader, cx, cy + dy, denied_tiles,
                current_z, z_arg, cx, cy,
                doors_passable=doors_passable, door_tiles=door_tiles,
            )
            if not (side1_ok and side2_ok):
                return False

        can_walk, new_z = _is_walkable(
            map_reader, nx, ny, denied_tiles,
            current_z, z_arg, cx, cy,
            doors_passable=doors_passable, door_tiles=door_tiles,
        )
        if not can_walk:
            return False

        if current_z is not None:
            z_at[(nx, ny)] = new_z
        cx, cy = nx, ny

    return True


def find_path(
    map_reader: "MapReader",
    sx: int,
    sy: int,
    tx: int,
    ty: int,
    max_steps: int = 200,
    denied_tiles: set[tuple[int, int]] | None = None,
    current_z: int | None = None,
    adjacent: bool = False,
    doors_passable: bool = True,
    door_tiles: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Smart pathfinding: tries fast algorithm first, falls back to thorough.

    1. Weighted A* (weight=1.5, max_steps) — fast, near-optimal
    2. If no path: standard A* (weight=1.0, max_steps*2) — thorough

    If adjacent=True, pathfinding succeeds when reaching any tile within 1
    Chebyshev distance of the target (useful for impassable targets like forges).

    If doors_passable=True (default), door tiles are treated as walkable during
    path planning. The movement code opens doors when the agent walks into them.
    """
    if sx == tx and sy == ty:
        return []

    if adjacent and max(abs(sx - tx), abs(sy - ty)) <= 1:
        return []

    # Try weighted A* first (faster)
    path = _astar_core(
        map_reader, sx, sy, tx, ty,
        max_steps=max_steps,
        denied_tiles=denied_tiles,
        current_z=current_z,
        heuristic_weight=1.5,
        adjacent=adjacent,
        doors_passable=doors_passable,
        door_tiles=door_tiles,
    )
    if path:
        return path

    # Fall back to standard A* with more steps
    return _astar_core(
        map_reader, sx, sy, tx, ty,
        max_steps=max_steps * 2,
        denied_tiles=denied_tiles,
        current_z=current_z,
        heuristic_weight=1.0,
        adjacent=adjacent,
        doors_passable=doors_passable,
        door_tiles=door_tiles,
    )
