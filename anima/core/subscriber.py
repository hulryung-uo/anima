"""Subscriber base class and built-in implementations.

Subscribers observe Avatar events through the EventBus.
Each subscriber declares which topics it cares about.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


class Subscriber(ABC):
    """Base class for EventBus subscribers."""

    @abstractmethod
    def topics(self) -> list[str]:
        """Topic patterns to subscribe to."""
        ...

    @abstractmethod
    def on_event(self, topic: str, data: dict[str, Any]) -> None:
        """Called when a matching event is published."""
        ...


class LogSubscriber(Subscriber):
    """Writes structured JSON log lines to a file."""

    def __init__(self, path: str | Path = "data/events.jsonl") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a")  # noqa: SIM115

    def topics(self) -> list[str]:
        return ["*"]

    def on_event(self, topic: str, data: dict[str, Any]) -> None:
        entry = {
            "ts": datetime.now().isoformat(),
            "topic": topic,
            **data,
        }
        self._file.write(json.dumps(entry, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class BufferSubscriber(Subscriber):
    """Keeps a ring buffer of recent events for UI display."""

    def __init__(self, max_events: int = 200) -> None:
        self._events: deque[tuple[float, str, dict]] = deque(maxlen=max_events)

    def topics(self) -> list[str]:
        return ["*"]

    def on_event(self, topic: str, data: dict[str, Any]) -> None:
        self._events.append((time.time(), topic, data))

    def recent(self, count: int = 50, topic_filter: str = "*") -> list[tuple[float, str, dict]]:
        """Get recent events, optionally filtered."""
        import fnmatch
        events = list(self._events)
        if topic_filter != "*":
            events = [e for e in events if fnmatch.fnmatch(e[1], topic_filter)]
        return events[-count:]


class MetricsSubscriber(Subscriber):
    """Collects quantitative metrics from events."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._window_start: float = time.time()

    def topics(self) -> list[str]:
        return ["action.*", "avatar.walk_*", "brain.*", "system.*"]

    def on_event(self, topic: str, data: dict[str, Any]) -> None:
        self._counters[topic] = self._counters.get(topic, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        """Get current metrics and reset window."""
        elapsed = time.time() - self._window_start
        result = {
            "window_seconds": elapsed,
            "counters": dict(self._counters),
        }
        self._counters.clear()
        self._window_start = time.time()
        return result
