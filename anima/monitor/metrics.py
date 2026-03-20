"""Metrics collector — tracks quantitative performance over time windows."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class WindowMetrics:
    """Metrics computed over a time window."""

    window_seconds: float = 600.0  # 10 minutes
    walk_confirmed: int = 0
    walk_denied: int = 0
    skill_success: int = 0
    skill_fail: int = 0
    chop_success: int = 0
    chop_fail: int = 0
    chop_depleted: int = 0
    craft_success: int = 0
    craft_fail: int = 0
    gold_earned: int = 0
    gold_spent: int = 0
    stuck_count: int = 0
    distance_moved: int = 0
    unique_positions: set = field(default_factory=set)
    skill_gains: list = field(default_factory=list)

    @property
    def walk_success_rate(self) -> float:
        total = self.walk_confirmed + self.walk_denied
        return self.walk_confirmed / total if total > 0 else 1.0

    @property
    def skill_success_rate(self) -> float:
        total = self.skill_success + self.skill_fail
        return self.skill_success / total if total > 0 else 0.0

    @property
    def chop_success_rate(self) -> float:
        total = self.chop_success + self.chop_fail + self.chop_depleted
        return self.chop_success / total if total > 0 else 0.0

    @property
    def minutes_elapsed(self) -> float:
        return self.window_seconds / 60.0

    @property
    def gold_per_minute(self) -> float:
        mins = self.minutes_elapsed
        return self.gold_earned / mins if mins > 0 else 0.0


class MetricsCollector:
    """Collects events and computes rolling window metrics."""

    def __init__(self, window_seconds: float = 600.0) -> None:
        self._window = window_seconds
        self._events: list[tuple[float, str, dict]] = []  # (timestamp, event, data)
        self._last_pos: tuple[int, int] | None = None

    def record(self, event: str, data: dict | None = None) -> None:
        """Record an event."""
        self._events.append((time.time(), event, data or {}))
        # Prune old events
        cutoff = time.time() - self._window * 2
        self._events = [(t, e, d) for t, e, d in self._events if t > cutoff]

    def get_window(self, seconds: float | None = None) -> WindowMetrics:
        """Compute metrics for the last N seconds."""
        window = seconds or self._window
        cutoff = time.time() - window
        m = WindowMetrics(window_seconds=window)

        for ts, event, data in self._events:
            if ts < cutoff:
                continue

            if event == "walk_confirmed":
                m.walk_confirmed += 1
                pos = data.get("pos")
                if pos:
                    m.unique_positions.add(pos)
                    if self._last_pos and pos != self._last_pos:
                        m.distance_moved += 1
                    self._last_pos = pos
            elif event == "walk_denied":
                m.walk_denied += 1
            elif event == "skill_success":
                m.skill_success += 1
            elif event == "skill_fail":
                m.skill_fail += 1
            elif event == "chop_success":
                m.chop_success += 1
            elif event == "chop_fail":
                m.chop_fail += 1
            elif event == "chop_depleted":
                m.chop_depleted += 1
            elif event == "craft_success":
                m.craft_success += 1
            elif event == "craft_fail":
                m.craft_fail += 1
            elif event == "gold_earned":
                m.gold_earned += data.get("amount", 0)
            elif event == "gold_spent":
                m.gold_spent += data.get("amount", 0)
            elif event == "stuck":
                m.stuck_count += 1
            elif event == "skill_gain":
                m.skill_gains.append(data)

        return m
