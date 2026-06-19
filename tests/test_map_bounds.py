"""Regression tests for off-map coordinate handling in MapReader.get_tile.

UO map0 spans [0, MAP_WIDTH) x [0, MAP_HEIGHT). Coordinates outside that range
have no land block. Python's floor-division/modulo silently produce a negative
block index whose chunk/block-in-chunk math wraps to a *wrong* block (or the
empty graphic-0 fallback), which is not impassable -> the off-map void used to
read as WALKABLE and A* would path off the edge of the world. get_tile must
instead report off-map tiles as impassable void.
"""

from __future__ import annotations

import pytest

from anima.map import (
    FLAG_IMPASSABLE,
    MAP_HEIGHT,
    MAP_WIDTH,
    MapReader,
)
from anima.pathfinding import find_path, path_is_traversable


@pytest.fixture
def reader() -> MapReader:
    # MapReader does all file I/O lazily; the out-of-bounds guard short-circuits
    # before any resource access, so a bare instance with dummy paths is enough.
    return MapReader(resource_dir="/nonexistent", data_dir="/nonexistent")


@pytest.mark.parametrize(
    "x,y",
    [
        (-1, 0),          # one tile west of the edge -> bx=-1, cx=7
        (0, -1),          # one tile north of the edge -> by=-1, cy=7
        (-1, -1),         # north-west corner void
        (MAP_WIDTH, 0),   # one tile east of the edge
        (0, MAP_HEIGHT),  # one tile south of the edge
        (-8, -8),         # a full block off the corner (block_num collision)
    ],
)
def test_off_map_tile_is_impassable_void(reader: MapReader, x: int, y: int) -> None:
    tile = reader.get_tile(x, y)
    # The land must be flagged impassable...
    assert tile.land.flags & FLAG_IMPASSABLE
    assert tile.land.impassable is True
    # ...so every walkability check treats the void as blocked.
    assert tile.walkable is False
    assert tile.passable is False
    can_walk, _ = tile.walkable_z(0)
    assert can_walk is False
    # No phantom statics leak in from a wrongly-indexed block.
    assert tile.statics == []
    # Coordinates are echoed back unchanged (no clamping of the query point).
    assert tile.x == x and tile.y == y


def test_in_bounds_corners_do_not_trip_the_guard(reader: MapReader) -> None:
    # The last valid tile must NOT be rejected as out-of-bounds. We can't read
    # real map data here (no resource files), but the guard must not fire, so
    # the call proceeds past the bounds check and into the lazy loader. The
    # missing UOP file surfaces as a FileNotFoundError, which proves the guard
    # let an in-bounds coordinate through rather than returning the void tile.
    for x, y in [(0, 0), (MAP_WIDTH - 1, MAP_HEIGHT - 1)]:
        with pytest.raises((FileNotFoundError, OSError)):
            reader.get_tile(x, y)


class _EdgeMap:
    """Minimal map: only x in [0, w) is walkable; everything else is void.

    Mirrors the real MapReader contract after the fix: off-map tiles report
    impassable. Used to prove pathfinding no longer escapes the world edge.
    """

    def __init__(self, w: int, h: int) -> None:
        self.w = w
        self.h = h

    def get_tile(self, x: int, y: int):
        from types import SimpleNamespace

        in_bounds = 0 <= x < self.w and 0 <= y < self.h
        impassable = not in_bounds
        return SimpleNamespace(
            x=x,
            y=y,
            land=SimpleNamespace(impassable=impassable),
            statics=[],
            walkable=not impassable,
            walkable_z=(lambda cz, ok=not impassable: (ok, cz)),
        )


def test_astar_does_not_path_off_the_map_edge() -> None:
    # Standing at the western edge (x=0); the only "free" route the planner
    # could be tempted into is x<0 (the void). With the void impassable there
    # is genuinely no path west, so find_path must return [] rather than a
    # route that steps off the map.
    m = _EdgeMap(w=4, h=4)
    path = find_path(m, 0, 1, -2, 1, current_z=0)
    assert path == []
    # And any westward step is rejected by traversability revalidation.
    assert path_is_traversable(m, 0, 1, [(-1, 1)], current_z=0) is False


def test_real_reader_void_blocks_pathfinding(reader: MapReader) -> None:
    # End-to-end with the real MapReader's bounds guard: a single step west
    # off the map edge is not traversable, because get_tile now reports the
    # void as impassable instead of the previous walkable graphic-0 fallback.
    assert (
        path_is_traversable(reader, 0, 100, [(-1, 100)], current_z=0) is False
    )
