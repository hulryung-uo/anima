"""Regression tests for TileInfo.walkable_z static-blocker handling.

The Z-aware walkability check (used by A* via pathfinding._is_walkable) must
treat an impassable, non-surface static occupying the agent's z-plane as a
blocker. ~1000 such statics in tiledata (lava, statues, webs, grapevines) have
height==0, so a naive `top_z > current_z` overlap test let a blocker sitting
flush at the agent's feet read as WALKABLE — A* would then route a path
straight through lava and the server would deny the step (or the agent would
stand in it). A zero-height blocker still occupies its own z-plane.
"""

from __future__ import annotations

from anima.map import (
    FLAG_BRIDGE,
    FLAG_IMPASSABLE,
    FLAG_SURFACE,
    LandTile,
    StaticItem,
    TileInfo,
)


def _tile(statics: list[StaticItem], land_z: int = 0, land_flags: int = 0) -> TileInfo:
    return TileInfo(
        x=0,
        y=0,
        land=LandTile(graphic=3, z=land_z, flags=land_flags),
        statics=statics,
    )


def test_height0_impassable_static_at_feet_blocks() -> None:
    # e.g. a lava tile (impassable, non-surface, height 0) sitting flush on the
    # ground the agent is standing on. It must block.
    lava = StaticItem(
        graphic=4846, x=0, y=0, z=0, hue=0,
        flags=FLAG_IMPASSABLE, height=0, name="lava",
    )
    tile = _tile([lava], land_z=0)
    can_walk, _ = tile.walkable_z(0)
    assert can_walk is False


def test_height0_impassable_static_below_does_not_block() -> None:
    # A height-0 impassable static well below the agent's feet (its z-plane
    # ends below us) must NOT block a tile the agent walks across above it.
    buried = StaticItem(
        graphic=4846, x=0, y=0, z=-20, hue=0,
        flags=FLAG_IMPASSABLE, height=0, name="lava",
    )
    tile = _tile([buried], land_z=0)
    can_walk, new_z = tile.walkable_z(0)
    assert can_walk is True
    assert new_z == 0  # stand on the land surface


def test_tall_impassable_static_at_feet_still_blocks() -> None:
    # Sanity: a normal-height blocker (a wall) at the feet still blocks, i.e.
    # the fix did not change the common case.
    wall = StaticItem(
        graphic=100, x=0, y=0, z=0, hue=0,
        flags=FLAG_IMPASSABLE, height=20, name="wall",
    )
    tile = _tile([wall], land_z=0)
    can_walk, _ = tile.walkable_z(0)
    assert can_walk is False


def test_walkable_surface_static_is_not_blocked() -> None:
    # A surface static (e.g. a cave floor) is walkable, not a blocker, even
    # though it is impassable — surface takes precedence.
    floor = StaticItem(
        graphic=200, x=0, y=0, z=0, hue=0,
        flags=FLAG_IMPASSABLE | FLAG_SURFACE, height=0, name="cave floor",
    )
    # Void land so the only standing surface is the static.
    tile = _tile([floor], land_z=0, land_flags=FLAG_IMPASSABLE)
    can_walk, new_z = tile.walkable_z(0)
    assert can_walk is True
    assert new_z == 0


def test_empty_ground_is_walkable() -> None:
    # No statics, walkable land — trivially walkable, stands at land z.
    tile = _tile([], land_z=5)
    can_walk, new_z = tile.walkable_z(5)
    assert can_walk is True
    assert new_z == 5
