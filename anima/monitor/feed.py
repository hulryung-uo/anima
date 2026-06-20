"""ActivityFeed — cross-cutting event bus for observability."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

Subscriber = Callable[["ActivityEvent"], Coroutine[Any, Any, None]]


@dataclass
class ActivityEvent:
    timestamp: float = field(default_factory=time.time)
    category: str = ""  # brain, action, skill, social, combat, movement, system
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    importance: int = 1  # 1=routine, 2=notable, 3=significant


class ActivityFeed:
    """Ring buffer of activity events with async subscriber notification."""

    def __init__(self, max_events: int = 200) -> None:
        self._events: deque[ActivityEvent] = deque(maxlen=max_events)
        self._subscribers: list[Subscriber] = []
        # Strong references to in-flight notify tasks. asyncio.create_task only
        # registers a *weak* reference in the event loop, so a fire-and-forget
        # task with no other referent can be garbage-collected mid-await — the
        # subscriber coroutine (web broadcast, file write, report) then silently
        # stops at its first suspension point and never finishes. Holding the
        # task here keeps it alive; the done-callback drops it once complete.
        self._tasks: set[asyncio.Task[None]] = set()

    def publish(
        self,
        category: str,
        message: str,
        details: dict[str, Any] | None = None,
        importance: int = 1,
    ) -> None:
        event = ActivityEvent(
            category=category,
            message=message,
            details=details or {},
            importance=importance,
        )
        self._events.append(event)
        for sub in self._subscribers:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                continue
            task = loop.create_task(sub(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        self._subscribers.remove(callback)

    def recent(self, count: int = 20) -> list[ActivityEvent]:
        # `events[-count:]` collapses to `events[0:]` (the WHOLE buffer) when
        # count == 0, and slices from the wrong end for negative counts.
        if count <= 0:
            return []
        events = list(self._events)
        return events[-count:]

    @property
    def total_count(self) -> int:
        return len(self._events)
