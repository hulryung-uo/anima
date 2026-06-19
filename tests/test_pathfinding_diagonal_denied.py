"""Regression: a denied perpendicular corner must not veto the diagonal.

``denied_tiles`` is the walker's *transient* cache of tiles the server most
recently refused — almost always because a wandering mobile briefly stood
there. The UO server's diagonal corner-cutting rule only consults real map
impassability (statics / land), so a mobile (or any denied tile) sitting on a
perpendicular corner does NOT forbid a diagonal step.

The old A* fed ``denied_tiles`` into the corner-cut side checks, so a single
denied corner became a phantom wall that rejected every diagonal through it.
When that diagonal was the only way around an obstacle, ``go_to`` replanned in
a loop and the agent wedged. The fix: the side checks ignore ``denied_tiles``
(the destination check still honors it).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from anima.pathfinding import find_path


@dataclass(slots=True)
class _Land:
    flags: int = 0

    @property
    def impassable(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass(slots=True)
class _Tile:
    x: int = 0
    y: int = 0
    land: _Land = field(default_factory=_Land)
    statics: list = field(default_factory=list)

    @property
    def walkable(self) -> bool:
        return not self.land.impassable


class _OpenMap:
    """Every tile is walkable on the map; blocking is expressed only via
    ``denied_tiles`` passed to find_path (i.e. the walker's deny cache)."""

    def get_tile(self, x: int, y: int) -> _Tile:
        return _Tile(x=x, y=y, land=_Land(flags=0))


def test_denied_corner_does_not_block_only_diagonal() -> None:
    # Start at (5,5), goal (6,6) — one SE diagonal away. Deny every other
    # tile adjacent to the goal so the SE diagonal is the ONLY way in.
    # The two perpendicular corners of that diagonal, (6,5) and (5,6), are
    # among the denied tiles. With the bug the corner-cut check rejected the
    # diagonal and A* had no route; the fix lets the diagonal through.
    mp = _OpenMap()
    denied = {
        (6, 5), (5, 6),          # the diagonal's perpendicular corners
        (7, 5), (7, 6), (7, 7),  # seal off every alternative approach to (6,6)
        (6, 7), (5, 7),
    }
    path = find_path(
        mp, 5, 5, 6, 6,
        max_steps=1000,
        denied_tiles=denied,
        current_z=None,
    )
    assert path == [(6, 6)], (
        "A* must take the SE diagonal even though both perpendicular corners "
        f"are in the transient denied cache; got {path}"
    )


def test_denied_destination_is_still_blocked() -> None:
    # The destination check must still honor denied_tiles: a denied GOAL-side
    # tile is genuinely off-limits. Here the direct diagonal target itself is
    # denied, so the path must route around it (never step onto (6,6)).
    mp = _OpenMap()
    path = find_path(
        mp, 5, 5, 7, 5,
        max_steps=1000,
        denied_tiles={(6, 5)},  # straight-line tile is denied
        current_z=None,
    )
    assert path, "expected a detour route to (7,5)"
    assert (6, 5) not in path, "must not step onto the denied tile"
    assert path[-1] == (7, 5)
