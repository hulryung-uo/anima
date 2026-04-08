"""Unified circuit-breaker / cooldown abstraction for planner state.

Replaces ad-hoc `depleted_banks`, `refused_vendors`, `_failed_destinations`,
`_craft_bs_material_cooldown`, `_ore_pickup_fails`, etc. patterns that
were scattered throughout the planner and its procedures.

Usage:
    breaker = CircuitBreaker(max_failures=3, cooldown_s=600)
    if not breaker.is_open(target_serial):
        result = await attempt(target_serial)
        if result.success:
            breaker.record_success(target_serial)
        else:
            breaker.record_failure(target_serial)
"""

from __future__ import annotations

import time
from typing import Any, Hashable


class CircuitBreaker:
    """Track failures per target and cool down after a threshold is hit.

    Each target is counted independently. When a target reaches
    `max_failures`, it becomes "open" for `cooldown_s` seconds, during
    which `is_open(target)` returns True. After cooldown expires the
    counter auto-resets.
    """

    def __init__(self, max_failures: int, cooldown_s: float) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be > 0")
        self._max = max_failures
        self._cooldown = cooldown_s
        # target -> [failure_count, tripped_at]
        self._state: dict[Hashable, list[float]] = {}

    def record_failure(self, target: Hashable) -> None:
        """Count one failure. Opens the breaker if max_failures reached."""
        entry = self._state.setdefault(target, [0, 0.0])
        entry[0] += 1
        if entry[0] >= self._max:
            entry[1] = time.time()

    def record_success(self, target: Hashable) -> None:
        """Reset counter and cooldown for a target."""
        self._state.pop(target, None)

    def trip(self, target: Hashable) -> None:
        """Open the breaker immediately, skipping the counter."""
        self._state[target] = [self._max, time.time()]

    def reset(self, target: Hashable) -> None:
        """Remove a target from tracking entirely."""
        self._state.pop(target, None)

    def reset_all(self) -> None:
        self._state.clear()

    def is_open(self, target: Hashable) -> bool:
        """True while the target is in its cooldown window."""
        entry = self._state.get(target)
        if not entry:
            return False
        count, tripped_at = entry
        if count < self._max:
            return False
        if time.time() - tripped_at >= self._cooldown:
            # Auto-expire
            self._state.pop(target, None)
            return False
        return True

    def failure_count(self, target: Hashable) -> int:
        entry = self._state.get(target)
        return entry[0] if entry else 0

    def open_targets(self) -> list[Hashable]:
        """List of targets whose breaker is currently open."""
        return [t for t in list(self._state.keys()) if self.is_open(t)]

    def snapshot(self) -> dict[str, Any]:
        """Diagnostic snapshot for logging."""
        now = time.time()
        return {
            "max_failures": self._max,
            "cooldown_s": self._cooldown,
            "tracked": len(self._state),
            "open": [
                {
                    "target": str(t),
                    "count": self._state[t][0],
                    "open_for_more_s": max(
                        0.0, self._cooldown - (now - self._state[t][1])
                    ),
                }
                for t in self.open_targets()
            ],
        }
