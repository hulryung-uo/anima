"""WalkerManager — movement state machine synced with SelfState."""

from __future__ import annotations

import asyncio
import time

from anima.perception.event_stream import EventStream, GameEventType
from anima.perception.self_state import SelfState

MAX_STEP_COUNT = 5
MAX_FAST_WALK_STACK_SIZE = 5
TURN_DELAY_MS = 100
WALK_DELAY_MS = 400
RUN_DELAY_MS = 200

# Denied tile cache
DENIED_TILE_EXPIRY_S = 60.0  # 1 minute — short so temporary blocks (mobiles) clear fast
MAX_DENIED_TILES = 2000

# Consecutive denial thresholds
CONSECUTIVE_DENIAL_WANDER = 3   # after 3 denials, give up goal and wander
CONSECUTIVE_DENIAL_COOLDOWN = 5  # after 5 denials, long cooldown
CONSECUTIVE_DENIAL_COOLDOWN_MS = 5000


class WalkerManager:
    """Mirrors ClassicUO's WalkerManager, syncs position to SelfState."""

    def __init__(self, self_state: SelfState, events: EventStream) -> None:
        self._self_state = self_state
        self._events = events
        self.walk_sequence: int = 0
        self.steps_count: int = 0
        self.walking_failed: bool = False
        self.last_step_time: float = 0.0
        self.fast_walk_keys: list[int] = [0] * MAX_FAST_WALK_STACK_SIZE

        # Denied tile cache: (x, y) -> timestamp
        self.denied_tiles: dict[tuple[int, int], float] = {}

        # Consecutive denial tracking
        self.consecutive_denials: int = 0
        self._denied_target: tuple[int, int] | None = None

        # Pending step tile — set before sending walk, cleared on confirm/deny
        self._pending_step_tile: tuple[int, int] | None = None

        # Set to True on deny — signals path cache should be invalidated
        self._path_dirty: bool = False

    def reset(self) -> None:
        self.steps_count = 0
        self.walk_sequence = 0
        self.walking_failed = False
        self.last_step_time = 0.0
        self.consecutive_denials = 0
        self._denied_target = None
        self._pending_step_tile = None

    def set_fast_walk_keys(self, keys: list[int]) -> None:
        for i in range(min(len(keys), MAX_FAST_WALK_STACK_SIZE)):
            self.fast_walk_keys[i] = keys[i]

    def add_fast_walk_key(self, key: int) -> None:
        for i in range(MAX_FAST_WALK_STACK_SIZE):
            if self.fast_walk_keys[i] == 0:
                self.fast_walk_keys[i] = key
                break

    def pop_fast_walk_key(self) -> int:
        for i in range(MAX_FAST_WALK_STACK_SIZE):
            key = self.fast_walk_keys[i]
            if key != 0:
                self.fast_walk_keys[i] = 0
                return key
        return 0

    def next_sequence(self) -> int:
        seq = self.walk_sequence
        if self.walk_sequence == 0xFF:
            self.walk_sequence = 1
        else:
            self.walk_sequence += 1
        return seq

    def confirm_walk(self, seq: int) -> None:
        if self.steps_count > 0:
            self.steps_count -= 1
        self.consecutive_denials = 0
        # Predictively update position to where this step was heading.
        # Without this, self_state stays at the old position after a
        # successful walk, causing pathfinding to calculate from the
        # wrong spot and cascading into more server denials.
        if self._pending_step_tile is not None:
            nx, ny = self._pending_step_tile
            self._self_state.x = nx
            self._self_state.y = ny
            self._events.emit(
                GameEventType.POSITION_CHANGED,
                {"x": nx, "y": ny, "z": self._self_state.z,
                 "direction": self._self_state.direction},
            )
        self._pending_step_tile = None
        self._events.emit(GameEventType.WALK_CONFIRMED, {"seq": seq})

    def deny_walk(self, seq: int, x: int, y: int, z: int, direction: int) -> None:
        # Record the denied tile if we know which one we tried
        if self._pending_step_tile is not None:
            self.record_denied_tile(*self._pending_step_tile)
            self._pending_step_tile = None

        self.steps_count = 0
        # Server resets state.Sequence to 0 on deny.
        # Next walk MUST send seq=0, otherwise server rejects with
        # (state.Sequence==0 && seq!=0) check in MovementReq.
        self.walk_sequence = 0
        self.walking_failed = False
        self.consecutive_denials += 1
        # Short cooldown — just enough for path recalculation
        self.last_step_time = asyncio.get_event_loop().time() * 1000 + 200
        # Invalidate path cache so next step recalculates with denied tile
        self._path_dirty = True
        self.sync_position(x, y, z, direction)
        self._events.emit(
            GameEventType.WALK_DENIED,
            {"seq": seq, "x": x, "y": y, "z": z},
        )

    def sync_position(self, x: int, y: int, z: int, direction: int) -> None:
        """Sync position to SelfState (called on MobileUpdate, DenyWalk, etc.)."""
        self._self_state.x = x
        self._self_state.y = y
        self._self_state.z = z
        self._self_state.direction = direction
        self._events.emit(
            GameEventType.POSITION_CHANGED,
            {"x": x, "y": y, "z": z, "direction": direction},
        )

    def can_walk(self) -> bool:
        now = asyncio.get_event_loop().time() * 1000
        return (
            not self.walking_failed
            and self.steps_count < MAX_STEP_COUNT
            and now >= self.last_step_time
        )

    # ------------------------------------------------------------------
    # Denied tile cache
    # ------------------------------------------------------------------

    def record_denied_tile(self, x: int, y: int) -> None:
        """Record a tile that was denied by the server."""
        self.denied_tiles[(x, y)] = time.time()
        # Prune if too many
        if len(self.denied_tiles) > MAX_DENIED_TILES:
            oldest = sorted(self.denied_tiles.items(), key=lambda t: t[1])
            for tile, _ in oldest[: len(self.denied_tiles) - MAX_DENIED_TILES]:
                del self.denied_tiles[tile]

    def is_tile_denied(self, x: int, y: int) -> bool:
        """Check if a tile is in the denied cache (not expired)."""
        ts = self.denied_tiles.get((x, y))
        if ts is None:
            return False
        if time.time() - ts >= DENIED_TILE_EXPIRY_S:
            del self.denied_tiles[(x, y)]
            return False
        return True

    def clear_denied_tile(self, x: int, y: int) -> None:
        """Remove a specific tile from the denied cache (e.g. after opening a door)."""
        self.denied_tiles.pop((x, y), None)

    def clear_all_denied_tiles(self) -> None:
        """Clear entire denied tile cache (e.g. after being stuck too long)."""
        self.denied_tiles.clear()

    # ------------------------------------------------------------------
    # Stuck detection
    # ------------------------------------------------------------------

    def check_stuck(self, target: tuple[int, int] | None) -> str:
        """Check if we're stuck trying to reach a target.

        Returns:
            "ok" — keep going
            "wander" — give up this path and try wandering
            "cooldown" — too many denials, long cooldown needed
        """
        if target != self._denied_target:
            self.consecutive_denials = 0
            self._denied_target = target

        if self.consecutive_denials >= CONSECUTIVE_DENIAL_COOLDOWN:
            return "cooldown"
        if self.consecutive_denials >= CONSECUTIVE_DENIAL_WANDER:
            return "wander"
        return "ok"
