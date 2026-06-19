"""Planner health metrics — fast loop detection.

Tracks the last N procedure selections in a sliding window and flags
a loop when the diversity (unique / total) drops below a threshold.

This is deliberately simple and synchronous — the planner calls
`record()` after each selection and checks `is_looping()` to decide
whether to break out of a suspected infinite loop.
"""

from __future__ import annotations

from collections import Counter, deque


class PlannerHealth:
    """Sliding-window planner selection tracker.

    Reports a loop when fewer than `min_diversity` fraction of the last
    `window` selections were unique.
    """

    def __init__(self, window: int = 20, min_diversity: float = 0.2) -> None:
        self._window = window
        self._min_diversity = min_diversity
        self._selections: deque[str] = deque(maxlen=window)
        self._skip_counts: Counter[str] = Counter()

    def record(self, procedure: str) -> None:
        """Record a procedure that was actually selected and run."""
        self._selections.append(procedure)

    def record_run(self, procedure: str, *, ran: bool) -> None:
        """Record a procedure ONLY if it actually ran this tick.

        Loop detection must reflect what the planner is *doing*, not what it
        last did. On an idle tick (the planner selected nothing — ``tick()``
        returned ``None``) the caller has no fresh procedure to report, and
        feeding the previous tick's stale procedure name into the window
        manufactures a phantom mono-procedure "loop": after ``window // 2``
        idle ticks ``is_looping()`` trips on a procedure that never ran, the
        productive-grind exemption rejects it (no successful result), and the
        planner takes a 60s health break that resets the idle-tick counter —
        starving the idle/deadlock escalation that is the actual recovery for a
        None-returning planner. Idle is already tracked by ``_idle_ticks``; it
        must not pollute the loop-detection window.
        """
        if ran and procedure:
            self._selections.append(procedure)

    def record_skip(self, procedure: str) -> None:
        """Record a procedure that was filtered out.

        Separate counter so spammy skips don't look like loop activity
        but we still know which procedures the planner keeps rejecting.
        """
        self._skip_counts[procedure] += 1

    def is_looping(self) -> bool:
        """True if the recent selection window shows low diversity."""
        total = len(self._selections)
        if total < self._window // 2:
            return False  # not enough data yet
        unique = len(set(self._selections))
        return (unique / total) < self._min_diversity

    def dominant_procedure(self) -> str | None:
        """Most-selected procedure in the current window, or None."""
        if not self._selections:
            return None
        return Counter(self._selections).most_common(1)[0][0]

    def reset(self) -> None:
        """Clear all state (used after a successful recovery)."""
        self._selections.clear()
        self._skip_counts.clear()

    def snapshot(self) -> dict:
        """Return a dict snapshot for logging / diagnostics."""
        return {
            "window_size": len(self._selections),
            "unique": len(set(self._selections)),
            "dominant": self.dominant_procedure(),
            "looping": self.is_looping(),
            "top_skips": dict(self._skip_counts.most_common(5)),
        }
