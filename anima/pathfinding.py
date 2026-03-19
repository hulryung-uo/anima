"""A* pathfinding on the UO map grid."""

from __future__ import annotations

import heapq
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.map import MapReader

# UO direction constants: N=0, NE=1, E=2, SE=3, S=4, SW=5, W=6, NW=7
# Deltas: (dx, dy) for each direction
DIRECTION_DELTAS: dict[int, tuple[int, int]] = {
    0: (0, -1),  # North
    1: (1, -1),  # NorthEast
    2: (1, 0),  # East
    3: (1, 1),  # SouthEast
    4: (0, 1),  # South
    5: (-1, 1),  # SouthWest
    6: (-1, 0),  # West
    7: (-1, -1),  # NorthWest
}

# Reverse lookup: (dx_sign, dy_sign) -> direction
_DELTA_TO_DIR: dict[tuple[int, int], int] = {v: k for k, v in DIRECTION_DELTAS.items()}

SQRT2 = math.sqrt(2)


def direction_to(fx: int, fy: int, tx: int, ty: int) -> int:
    """Return the UO direction (0-7) from (fx,fy) to (tx,ty).

    Returns 0 (North) if positions are identical.
    """
    dx = tx - fx
    dy = ty - fy
    if dx == 0 and dy == 0:
        return 0
    # Normalize to -1/0/1
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    return _DELTA_TO_DIR.get((sx, sy), 0)


def _octile_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    return max(dx, dy) + (SQRT2 - 1) * min(dx, dy)


def find_path(
    map_reader: MapReader,
    sx: int,
    sy: int,
    tx: int,
    ty: int,
    max_steps: int = 200,
    denied_tiles: set[tuple[int, int]] | None = None,
    current_z: int | None = None,
) -> list[tuple[int, int]]:
    """A* pathfinding from (sx,sy) to (tx,ty).

    Args:
        current_z: If provided, enables Z-aware walkability checks.
            Tiles that are not reachable from the current Z level
            (step height > 16) will be treated as impassable.

    Returns a list of (x, y) waypoints excluding start, including target.
    Returns empty list if no path found within max_steps.
    """
    if sx == tx and sy == ty:
        return []

    # Priority queue: (f_score, counter, x, y)
    counter = 0
    open_set: list[tuple[float, int, int, int]] = []
    heapq.heappush(open_set, (_octile_distance(sx, sy, tx, ty), counter, sx, sy))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], float] = {(sx, sy): 0.0}
    closed: set[tuple[int, int]] = set()

    # Track Z at each visited node for Z-aware mode
    z_at: dict[tuple[int, int], int] = {}
    if current_z is not None:
        z_at[(sx, sy)] = current_z

    while open_set:
        _, _, cx, cy = heapq.heappop(open_set)

        if cx == tx and cy == ty:
            # Reconstruct path
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

            if denied_tiles and (nx, ny) in denied_tiles:
                continue

            tile = map_reader.get_tile(nx, ny)

            if current_z is not None:
                # Z-aware walkability
                node_z = z_at.get((cx, cy), current_z)
                can_walk, new_z = tile.walkable_z(node_z)
                if not can_walk:
                    continue
            else:
                if not tile.walkable:
                    continue
                new_z = 0  # unused

            # Diagonal moves cost sqrt(2), cardinal moves cost 1
            move_cost = SQRT2 if (dx != 0 and dy != 0) else 1.0
            tentative_g = g_score[(cx, cy)] + move_cost

            if tentative_g < g_score.get((nx, ny), float("inf")):
                came_from[(nx, ny)] = (cx, cy)
                g_score[(nx, ny)] = tentative_g
                if current_z is not None:
                    z_at[(nx, ny)] = new_z
                f = tentative_g + _octile_distance(nx, ny, tx, ty)
                counter += 1
                heapq.heappush(open_set, (f, counter, nx, ny))

    return []  # No path found
