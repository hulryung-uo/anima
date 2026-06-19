"""Tests for A* pathfinding."""

from __future__ import annotations

from dataclasses import dataclass, field

from anima.pathfinding import (
    DIRECTION_DELTAS,
    SQRT2,
    _octile_distance,
    direction_to,
    find_path,
    path_is_traversable,
)

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
        assert len(path) == 3  # 8-directional: 3 diagonal SE moves
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

    def test_diagonal_blocked_by_corner(self) -> None:
        """Diagonal moves blocked when a perpendicular tile is impassable."""
        m = MockMapReader()
        # Block the east tile so NE diagonal from (0,0) is not allowed
        m.block(1, 0)
        path = find_path(m, 0, 0, 1, -1)  # NE target
        assert len(path) > 0
        assert path[-1] == (1, -1)
        # Path must avoid direct NE (corner-cutting); goes around instead
        assert (1, 0) not in path

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

    def test_octile_heuristic_values_diagonal_aware(self) -> None:
        """The heuristic must reflect SQRT2 diagonal cost, not Manhattan.

        For a pure diagonal the cost-to-go is min(dx,dy)*SQRT2, NOT dx+dy.
        Manhattan (the previous, misnamed implementation) overstated this.
        """
        # Pure diagonal 3x3: true octile == 3*SQRT2, Manhattan would be 6.
        assert _octile_distance(0, 0, 3, 3) == 3 * SQRT2
        # Mixed: dx=4, dy=2 -> 2 diagonals + 2 cardinals = 2*SQRT2 + 2.
        assert _octile_distance(0, 0, 4, 2) == 2 * SQRT2 + 2.0
        # Pure cardinal stays integral.
        assert _octile_distance(0, 0, 5, 0) == 5.0

    def test_prefers_fewer_walk_steps_over_l_detour(self) -> None:
        """Regression: a diagonal route must beat an equal-Manhattan-length
        cardinal staircase in *walk-step count*.

        With the old cost model (diagonal cost == 2 == two cardinals) A* was
        indifferent, and on this obstacle layout it returned an 8-step path
        where a 7-step diagonal route exists. Each saved step is one fewer
        UO walk packet (and ~one throttle interval of travel time).
        """
        m = MockMapReader()
        for bx, by in [(0, 1), (1, 0), (1, 5), (2, 1), (2, 8), (3, 6), (5, 0)]:
            m.block(bx, by)
        path = find_path(m, 0, 0, 3, 4)
        assert path, "expected a path to (3, 4)"
        assert path[-1] == (3, 4)
        for x, y in path:
            assert (x, y) not in m.blocked
        # BFS gives the true minimum walk-step count for this layout.
        assert len(path) == _min_walk_steps(m, 0, 0, 3, 4)


class TestPathIsTraversable:
    """path_is_traversable re-validates a cached path against the live map."""

    def test_empty_path_is_traversable(self) -> None:
        m = MockMapReader()
        assert path_is_traversable(m, 5, 5, []) is True

    def test_clear_path_stays_valid(self) -> None:
        m = MockMapReader()
        path = find_path(m, 0, 0, 3, 0)
        assert path_is_traversable(m, 0, 0, path) is True

    def test_tile_blocked_mid_path_invalidates(self) -> None:
        """A tile that becomes impassable after planning fails revalidation."""
        m = MockMapReader()
        path = find_path(m, 0, 0, 4, 0)
        assert path_is_traversable(m, 0, 0, path) is True
        # A mob/static now occupies a tile on the route.
        m.block(*path[2])
        assert path_is_traversable(m, 0, 0, path) is False

    def test_denied_tile_invalidates(self) -> None:
        """A DenyWalk-recorded tile on the route invalidates the path."""
        m = MockMapReader()
        path = find_path(m, 0, 0, 4, 0)
        blocked_tile = path[1]
        assert path_is_traversable(m, 0, 0, path) is True
        assert (
            path_is_traversable(m, 0, 0, path, denied_tiles={blocked_tile})
            is False
        )

    def test_diagonal_corner_cut_invalidates(self) -> None:
        """A diagonal hop whose corner became blocked is not traversable."""
        m = MockMapReader()
        # A clean NE diagonal path.
        path = [(1, -1), (2, -2)]
        assert path_is_traversable(m, 0, 0, path) is True
        # Block a perpendicular corner of the first diagonal step.
        m.block(1, 0)  # east tile of the (0,0)->(1,-1) NE move
        assert path_is_traversable(m, 0, 0, path) is False

    def test_non_contiguous_path_rejected(self) -> None:
        """A path with a >1 tile jump (stale/teleported) is not traversable."""
        m = MockMapReader()
        assert path_is_traversable(m, 0, 0, [(2, 0)]) is False

    def test_zero_length_hop_rejected(self) -> None:
        m = MockMapReader()
        assert path_is_traversable(m, 0, 0, [(0, 0)]) is False

    def test_matches_find_path_corner_rules(self) -> None:
        """Any path find_path returns must validate as traversable."""
        m = MockMapReader()
        for bx, by in [(2, 0), (2, 1), (1, 2)]:
            m.block(bx, by)
        path = find_path(m, 0, 0, 4, 0)
        assert path
        assert path_is_traversable(m, 0, 0, path) is True


def _min_walk_steps(m: "MockMapReader", sx: int, sy: int, tx: int, ty: int) -> int:
    """Minimum number of 8-directional walk steps, honoring corner-cut rules."""
    from collections import deque

    def walkable(x: int, y: int) -> bool:
        return (x, y) not in m.blocked

    q: deque[tuple[int, int, int]] = deque([(sx, sy, 0)])
    seen: set[tuple[int, int]] = {(sx, sy)}
    while q:
        x, y, d = q.popleft()
        if (x, y) == (tx, ty):
            return d
        for dx, dy in DIRECTION_DELTAS.values():
            nx, ny = x + dx, y + dy
            if (nx, ny) in seen:
                continue
            if dx != 0 and dy != 0 and not (walkable(x + dx, y) and walkable(x, y + dy)):
                continue
            if not walkable(nx, ny):
                continue
            seen.add((nx, ny))
            q.append((nx, ny, d + 1))
    raise AssertionError("target unreachable in mock map")


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

    def test_confirm_resets_consecutive_only_on_move(self) -> None:
        w = self._make_walker()
        w.deny_walk(1, 10, 20, 0, 0)
        w.deny_walk(2, 10, 20, 0, 0)
        # Turn-only confirm (no pending step) should NOT reset denials
        w.steps_count = 1
        w.confirm_walk(3)
        assert w.consecutive_denials == 2
        # Actual move confirm (with pending step) resets denials
        w._pending_step_tile = (11, 20)
        w.steps_count = 1
        w.confirm_walk(4)
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

    def test_deny_walk_no_running_loop(self) -> None:
        """deny_walk must not raise when called without a running event loop.

        On Python 3.12+ ``asyncio.get_event_loop()`` raises outside a running
        loop, which previously broke deny handling in synchronous callers.
        """
        w = self._make_walker()
        w.steps_count = 3
        # Must not raise RuntimeError("no current event loop") and must
        # arm a future cooldown (last_step_time pushed into the future).
        w.deny_walk(5, 300, 400, 10, 2)
        assert w.steps_count == 0
        assert w.last_step_time > 0

    def test_can_walk_no_running_loop(self) -> None:
        """can_walk must not raise when called without a running event loop."""
        w = self._make_walker()
        # Fresh walker: no pending steps, no cooldown -> walkable.
        assert w.can_walk() is True

    def test_now_ms_falls_back_without_loop(self) -> None:
        """_now_ms returns a monotonic ms value when no loop is running."""
        from anima.perception.walker import _now_ms
        a = _now_ms()
        b = _now_ms()
        assert isinstance(a, float)
        # Monotonic: never goes backwards.
        assert b >= a
