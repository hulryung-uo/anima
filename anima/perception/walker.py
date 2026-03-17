"""WalkerManager — movement state machine synced with SelfState."""

from __future__ import annotations

import asyncio

from anima.perception.event_stream import EventStream, GameEventType
from anima.perception.self_state import SelfState

MAX_STEP_COUNT = 5
MAX_FAST_WALK_STACK_SIZE = 5
TURN_DELAY_MS = 100
WALK_DELAY_MS = 400
RUN_DELAY_MS = 200


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

    def reset(self) -> None:
        self.steps_count = 0
        self.walk_sequence = 0
        self.walking_failed = False
        self.last_step_time = 0.0

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
        self._events.emit(GameEventType.WALK_CONFIRMED, {"seq": seq})

    def deny_walk(self, seq: int, x: int, y: int, z: int, direction: int) -> None:
        self.steps_count = 0
        self.walking_failed = False  # allow retry
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
