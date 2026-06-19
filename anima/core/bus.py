"""EventBus — topic-based pub/sub for Avatar events.

Topics use dot-separated namespaces:
  avatar.position, avatar.health, avatar.skill_change
  action.start, action.end, action.walk
  brain.think, brain.goal_set, brain.goal_arrived
  system.error, system.metric

Subscribers can use wildcards:
  "avatar.*" — all avatar events
  "*" — everything
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections import defaultdict
from collections.abc import Callable as CallableABC
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    """A single event on the bus."""

    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# Callback type: receives (topic, data)
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class Subscription:
    """Handle to an active subscription."""

    id: int
    pattern: str
    callback: EventCallback


class EventBus:
    """Topic-based publish/subscribe event bus.

    Synchronous by default — callbacks run immediately in publish().
    Suitable for single-process, single-thread Avatar.
    """

    def __init__(self) -> None:
        self._next_id: int = 0
        self._subs: dict[int, Subscription] = {}
        # pattern → list of subscription ids (for fast lookup)
        self._pattern_index: dict[str, list[int]] = defaultdict(list)
        # Recent events ring buffer for snapshot/replay
        self._history: list[Event] = []
        self._history_max: int = 500

    def subscribe(self, pattern: str, callback: EventCallback) -> Subscription:
        """Subscribe to events matching pattern.

        Pattern examples:
          "avatar.position" — exact topic
          "avatar.*" — all avatar events
          "action.*" — all action events
          "*" — everything
        """
        self._next_id += 1
        sub = Subscription(id=self._next_id, pattern=pattern, callback=callback)
        self._subs[sub.id] = sub
        self._pattern_index[pattern].append(sub.id)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove a subscription."""
        self._subs.pop(sub.id, None)
        ids = self._pattern_index.get(sub.pattern, [])
        if sub.id in ids:
            ids.remove(sub.id)

    def publish(self, topic: str, data: dict[str, Any] | None = None) -> None:
        """Publish an event to all matching subscribers."""
        event = Event(topic=topic, data=data or {})

        # Store in history
        self._history.append(event)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max:]

        # Dispatch to matching subscribers. Iterate over a snapshot: callbacks
        # run synchronously inside publish() and several of them subscribe or
        # unsubscribe in response (one-shot listeners, procedures that tear down
        # their bus hooks on a terminal event, wait_for_condition). Mutating
        # self._subs while iterating its live .values() raises
        # "dictionary changed size during iteration", which would propagate out
        # of publish() and crash whatever published the event (a packet handler
        # or a running procedure). The snapshot keeps dispatch crash-free.
        for sub in list(self._subs.values()):
            if fnmatch.fnmatch(topic, sub.pattern):
                try:
                    sub.callback(topic, event.data)
                except Exception:
                    pass  # Never let a subscriber crash the bus

    def recent(self, count: int = 50, topic_filter: str = "*") -> list[Event]:
        """Get recent events, optionally filtered by topic pattern."""
        if topic_filter == "*":
            return self._history[-count:]
        return [
            e for e in self._history[-count * 2:]
            if fnmatch.fnmatch(e.topic, topic_filter)
        ][-count:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # --- Event-driven wait (v2 migration Step 2) ---

    async def wait_for_condition(
        self,
        predicate: CallableABC[[], bool],
        timeout: float = 5.0,
    ) -> bool:
        """Wait until predicate() returns True, or timeout expires.

        Event-driven: wakes on every bus event and rechecks the predicate.
        Returns True if predicate was satisfied, False on timeout.
        """
        if predicate():
            return True

        event = asyncio.Event()

        def _check(_topic: str, _data: dict) -> None:
            if predicate():
                event.set()

        sub = self.subscribe("*", _check)
        try:
            # Check again after subscribing (race window)
            if predicate():
                return True
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.unsubscribe(sub)
