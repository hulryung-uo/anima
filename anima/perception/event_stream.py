"""Event stream: ring buffer of game events for the brain layer to poll."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto

MAX_EVENTS = 200


class GameEventType(Enum):
    MOBILE_APPEARED = auto()
    MOBILE_MOVED = auto()
    MOBILE_REMOVED = auto()
    ITEM_APPEARED = auto()
    ITEM_REMOVED = auto()
    SPEECH_HEARD = auto()
    STATS_CHANGED = auto()
    HP_CHANGED = auto()
    MANA_CHANGED = auto()
    STAM_CHANGED = auto()
    SKILL_CHANGED = auto()
    WALK_CONFIRMED = auto()
    WALK_DENIED = auto()
    POSITION_CHANGED = auto()
    TARGET_REQUESTED = auto()
    DAMAGE_DEALT = auto()
    DAMAGE_TAKEN = auto()
    GUMP_OPENED = auto()
    GUMP_CLOSED = auto()


@dataclass
class GameEvent:
    type: GameEventType
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventStream:
    """Ring buffer of game events. Brain calls poll() each tick."""

    def __init__(self) -> None:
        self._events: deque[GameEvent] = deque(maxlen=MAX_EVENTS)
        self._read_index: int = 0
        self._write_index: int = 0

    def emit(self, event_type: GameEventType, data: dict | None = None) -> None:
        event = GameEvent(type=event_type, data=data or {})
        self._events.append(event)
        self._write_index += 1

    def poll(self) -> list[GameEvent]:
        """Return all unread events and advance the read cursor."""
        available = self._write_index - self._read_index
        if available <= 0:
            return []
        # If more events were produced than buffer size, we lost some
        buffer_len = len(self._events)
        if available > buffer_len:
            available = buffer_len
            self._read_index = self._write_index - buffer_len
        start = buffer_len - (self._write_index - self._read_index)
        events = list(self._events)[start:]
        self._read_index = self._write_index
        return events

    def peek(self, count: int = 1) -> list[GameEvent]:
        """Peek at the latest events without advancing the cursor."""
        events = list(self._events)
        return events[-count:]

    @property
    def pending_count(self) -> int:
        available = self._write_index - self._read_index
        return max(0, min(available, len(self._events)))
