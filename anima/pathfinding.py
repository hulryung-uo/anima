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
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    return max(dx, dy) + (SQRT2 - 1) * min(dx, dy)


def _is_walkable(
    map_reader: "MapReader",
    x: int, y: int,
    denied_tiles: set[tuple[int, int]] | None,
    current_z: int | None,
    z_at: dict[tuple[int, int], int] | None,
    cx: int, cy: int,
) -> tuple[bool, int]:
    """Check if tile (x,y) is walkable. Returns (can_walk, new_z)."""
    if denied_tiles and (x, y) in denied_tiles:
        return False, 0

    tile = map_reader.get_tile(x, y)

    if current_z is not None and z_at is not None:
        node_z = z_at.get((cx, cy), current_z)
        return tile.walkable_z(node_z)
    else:
        return tile.walkable, 0


def _astar_core(
    map_reader: "MapReader",
    sx: int, sy: int,
    tx: int, ty: int,
    max_steps: int,
    denied_tiles: set[tuple[int, int]] | None,
    current_z: int | None,
    heuristic_weight: float = 1.0,
) -> list[tuple[int, int]]:
    """Core A* implementation with configurable heuristic weight.

    weight=1.0: standard A* (optimal)
    weight>1.0: weighted A* (faster, near-optimal)
    weight=inf: greedy best-first (fastest, not optimal)
    """
    if sx == tx and sy == ty:
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

        if cx == tx and cy == ty:
            path: list[tuple[int, int]] = []
            node = (tx, ty)
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

        for direction in range(8):
            dx, dy = DIRECTION_DELTAS[direction]
            nx, ny = cx + dx, cy + dy

            if (nx, ny) in closed:
                continue

            can_walk, new_z = _is_walkable(
                map_reader, nx, ny, denied_tiles, current_z, z_at if current_z else None, cx, cy,
            )
            if not can_walk:
                continue

            move_cost = SQRT2 if (dx != 0 and dy != 0) else 1.0
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


def find_path(
    map_reader: "MapReader",
    sx: int,
    sy: int,
    tx: int,
    ty: int,
    max_steps: int = 200,
    denied_tiles: set[tuple[int, int]] | None = None,
    current_z: int | None = None,
) -> list[tuple[int, int]]:
    """Smart pathfinding: tries fast algorithm first, falls back to thorough.

    1. Weighted A* (weight=1.5, max_steps) — fast, near-optimal
    2. If no path: standard A* (weight=1.0, max_steps*2) — thorough
    """
    if sx == tx and sy == ty:
        return []

    # Try weighted A* first (faster)
    path = _astar_core(
        map_reader, sx, sy, tx, ty,
        max_steps=max_steps,
        denied_tiles=denied_tiles,
        current_z=current_z,
        heuristic_weight=1.5,
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
    )
