"""Tests for A* pathfinding."""

from __future__ import annotations

from dataclasses import dataclass, field

from anima.pathfinding import DIRECTION_DELTAS, direction_to, find_path

# ---------------------------------------------------------------------------
# Mock map reader for testing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MockLandTile:
    graphic: int = 0
    z: int = 0
    flags: int = 0

    @property
    def impassable(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass(slots=True)
class MockTileInfo:
    x: int = 0
    y: int = 0
    land: MockLandTile = field(default_factory=MockLandTile)
    statics: list = field(default_factory=list)

    @property
    def walkable(self) -> bool:
        if self.land.impassable:
            return False
        return True


class MockMapReader:
    """Simple grid-based mock map reader for testing pathfinding."""

    def __init__(self, width: int = 20, height: int = 20) -> None:
        self.width = width
        self.height = height
        self.blocked: set[tuple[int, int]] = set()

    def block(self, x: int, y: int) -> None:
        self.blocked.add((x, y))

    def get_tile(self, x: int, y: int) -> MockTileInfo:
        flags = 0x40 if (x, y) in self.blocked else 0
        return MockTileInfo(
            x=x,
            y=y,
            land=MockLandTile(flags=flags),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDirectionTo:
    def test_north(self) -> None:
        assert direction_to(5, 5, 5, 4) == 0  # North

    def test_east(self) -> None:
        assert direction_to(5, 5, 6, 5) == 2  # East

    def test_south(self) -> None:
        assert direction_to(5, 5, 5, 6) == 4  # South

    def test_west(self) -> None:
        assert direction_to(5, 5, 4, 5) == 6  # West

    def test_northeast(self) -> None:
        assert direction_to(5, 5, 6, 4) == 1  # NorthEast

    def test_southwest(self) -> None:
        assert direction_to(5, 5, 4, 6) == 5  # SouthWest

    def test_same_position(self) -> None:
        assert direction_to(5, 5, 5, 5) == 0  # Default North

    def test_large_delta_normalizes(self) -> None:
        # Direction should normalize large deltas to -1/0/1
        assert direction_to(0, 0, 10, 10) == 3  # SouthEast


class TestDirectionDeltas:
    def test_all_eight_directions(self) -> None:
        assert len(DIRECTION_DELTAS) == 8
        for d in range(8):
            dx, dy = DIRECTION_DELTAS[d]
            assert -1 <= dx <= 1
            assert -1 <= dy <= 1


class TestFindPath:
    def test_already_at_target(self) -> None:
        m = MockMapReader()
        path = find_path(m, 5, 5, 5, 5)
        assert path == []

    def test_straight_line(self) -> None:
        m = MockMapReader()
        path = find_path(m, 0, 0, 3, 0)
        assert len(path) == 3
        assert path[-1] == (3, 0)
        # Should be a straight line east
        for i, (x, y) in enumerate(path):
            assert x == i + 1
            assert y == 0

    def test_diagonal(self) -> None:
        m = MockMapReader()
        path = find_path(m, 0, 0, 3, 3)
        assert len(path) == 3
        assert path[-1] == (3, 3)

    def test_obstacle_avoidance(self) -> None:
        m = MockMapReader()
        # Create a wall blocking direct east path
        m.block(2, 0)
        m.block(2, 1)
        path = find_path(m, 0, 0, 4, 0)
        assert len(path) > 0
        assert path[-1] == (4, 0)
        # Path should not include blocked tiles
        for x, y in path:
            assert (x, y) not in m.blocked

    def test_no_path(self) -> None:
        m = MockMapReader()
        # Completely surround target
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                m.block(5 + dx, 5 + dy)
        path = find_path(m, 0, 0, 5, 5)
        assert path == []

    def test_respects_max_steps(self) -> None:
        m = MockMapReader()
        # Very far target with small max_steps
        path = find_path(m, 0, 0, 100, 100, max_steps=10)
        assert path == []

    def test_path_around_u_shaped_wall(self) -> None:
        m = MockMapReader()
        # U-shaped wall: blocks going east, must go around
        for y in range(0, 5):
            m.block(3, y)
        for x in range(3, 6):
            m.block(x, 5)
        path = find_path(m, 0, 2, 5, 2)
        assert len(path) > 0
        assert path[-1] == (5, 2)
        for x, y in path:
            assert (x, y) not in m.blocked
