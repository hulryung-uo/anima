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

    def test_denied_tiles_avoided(self) -> None:
        m = MockMapReader()
        denied = {(1, 0), (1, 1)}
        path = find_path(m, 0, 0, 3, 0, denied_tiles=denied)
        assert len(path) > 0
        assert path[-1] == (3, 0)
        for x, y in path:
            assert (x, y) not in denied

    def test_denied_tiles_none_default(self) -> None:
        m = MockMapReader()
        # No denied tiles — same behavior as before
        path = find_path(m, 0, 0, 3, 0, denied_tiles=None)
        assert len(path) == 3
        assert path[-1] == (3, 0)

    def test_denied_tiles_blocks_all_paths(self) -> None:
        m = MockMapReader()
        # Deny all tiles around target
        denied = set()
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                denied.add((5 + dx, 5 + dy))
        path = find_path(m, 0, 0, 5, 5, denied_tiles=denied)
        assert path == []

    def test_denied_tiles_forces_detour(self) -> None:
        m = MockMapReader()
        # Deny the direct diagonal path, forcing a detour
        denied = {(1, 1), (2, 2)}
        path = find_path(m, 0, 0, 3, 3, denied_tiles=denied)
        assert len(path) > 0
        assert path[-1] == (3, 3)
        for x, y in path:
            assert (x, y) not in denied


class TestWalkerDeniedTiles:
    """Test WalkerManager denied tile cache and stuck detection."""

    def _make_walker(self):
        from anima.perception.event_stream import EventStream
        from anima.perception.self_state import SelfState
        from anima.perception.walker import WalkerManager
        ss = SelfState(serial=1)
        events = EventStream()
        return WalkerManager(ss, events)

    def test_record_and_check_denied(self) -> None:
        w = self._make_walker()
        w.record_denied_tile(10, 20)
        assert w.is_tile_denied(10, 20)
        assert not w.is_tile_denied(10, 21)

    def test_denied_tile_expiry(self) -> None:
        import time as _time
        from anima.perception.walker import DENIED_TILE_EXPIRY_S
        w = self._make_walker()
        # Record with an old timestamp
        w.denied_tiles[(5, 5)] = _time.time() - DENIED_TILE_EXPIRY_S - 1
        assert not w.is_tile_denied(5, 5)
        assert (5, 5) not in w.denied_tiles  # should be cleaned up

    def test_clear_denied_tile(self) -> None:
        w = self._make_walker()
        w.record_denied_tile(10, 20)
        w.clear_denied_tile(10, 20)
        assert not w.is_tile_denied(10, 20)

    def test_deny_walk_records_pending_tile(self) -> None:
        w = self._make_walker()
        w._pending_step_tile = (15, 25)
        w.deny_walk(1, 10, 20, 0, 0)
        assert w.is_tile_denied(15, 25)
        assert w._pending_step_tile is None

    def test_confirm_walk_clears_pending(self) -> None:
        w = self._make_walker()
        w.steps_count = 1
        w._pending_step_tile = (15, 25)
        w.confirm_walk(1)
        assert w._pending_step_tile is None
        assert not w.is_tile_denied(15, 25)

    def test_consecutive_denials_increment(self) -> None:
        w = self._make_walker()
        w.deny_walk(1, 10, 20, 0, 0)
        w.deny_walk(2, 10, 20, 0, 0)
        w.deny_walk(3, 10, 20, 0, 0)
        assert w.consecutive_denials == 3

    def test_confirm_resets_consecutive(self) -> None:
        w = self._make_walker()
        w.deny_walk(1, 10, 20, 0, 0)
        w.deny_walk(2, 10, 20, 0, 0)
        w.steps_count = 1
        w.confirm_walk(3)
        assert w.consecutive_denials == 0

    def test_check_stuck_ok(self) -> None:
        w = self._make_walker()
        assert w.check_stuck((100, 200)) == "ok"

    def test_check_stuck_wander(self) -> None:
        w = self._make_walker()
        w.consecutive_denials = 3
        w._denied_target = (100, 200)
        assert w.check_stuck((100, 200)) == "wander"

    def test_check_stuck_cooldown(self) -> None:
        w = self._make_walker()
        w.consecutive_denials = 5
        w._denied_target = (100, 200)
        assert w.check_stuck((100, 200)) == "cooldown"

    def test_check_stuck_resets_on_new_target(self) -> None:
        w = self._make_walker()
        w.consecutive_denials = 4
        w._denied_target = (100, 200)
        assert w.check_stuck((300, 400)) == "ok"
        assert w.consecutive_denials == 0

    def test_max_denied_tiles_pruned(self) -> None:
        from anima.perception.walker import MAX_DENIED_TILES
        w = self._make_walker()
        for i in range(MAX_DENIED_TILES + 50):
            w.record_denied_tile(i, 0)
        assert len(w.denied_tiles) <= MAX_DENIED_TILES
