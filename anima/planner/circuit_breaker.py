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
        # Kept as two parallel dicts so each has a precise type.
        self._counts: dict[Hashable, int] = {}
        self._tripped_at: dict[Hashable, float] = {}
        # Targets whose cooldown has lapsed and are being probed once.
        self._half_open: set[Hashable] = set()
        # Wall-clock of the last mutating touch (failure/trip) per target.
        # Without this, a target that is tripped once and then NEVER revisited
        # (the dominant case for ephemeral keys: a depleted 8x8 ore-bank cell
        # the miner roams away from, a one-shot ore-pile serial that despawns)
        # lingers in ``_counts``/``_tripped_at`` forever. The half-open
        # recovery branch in ``is_open`` only evicts it if that exact key is
        # polled again after its cooldown lapses, which never happens for a
        # key that is gone from the world. Over a long mining/crafting session
        # these breakers (``_bank_breaker``, ``_ore_pickup_breaker``) accrue one
        # permanent entry per distinct key ever seen — a real unbounded-growth
        # leak. ``_last_touch`` lets ``prune_expired`` drop keys idle past the
        # cooldown window (re-encountering one later just starts fresh, which is
        # correct: a bank untouched for > cooldown_s has refilled).
        self._last_touch: dict[Hashable, float] = {}

    def _drop(self, target: Hashable) -> None:
        self._counts.pop(target, None)
        self._tripped_at.pop(target, None)
        self._half_open.discard(target)
        self._last_touch.pop(target, None)

    def record_failure(self, target: Hashable) -> None:
        """Count one failure. Opens the breaker if max_failures reached."""
        self._last_touch[target] = time.time()
        # A failed half-open probe means the target is still bad: re-trip
        # immediately rather than counting up from zero again.
        if target in self._half_open:
            self.trip(target)
            self._sweep(exclude=target)
            return
        count = self._counts.get(target, 0) + 1
        self._counts[target] = count
        if count >= self._max:
            now = time.time()
            prev = self._tripped_at.get(target)
            # Stamp the cooldown clock on the *transition* into open
            # (prev is None) or when re-tripping after an already-lapsed
            # window that was never polled (now - prev >= cooldown). A
            # failure that lands while the breaker is STILL inside its
            # cooldown must NOT push the window forward: otherwise a target
            # that keeps failing under sustained pressure (e.g. a directly
            # fetched survival heal interrupted every tick by an adjacent
            # mob, or a watchdog-cancelled procedure) resets its own
            # cooldown on every attempt and the breaker NEVER reaches the
            # half-open recovery probe — it stays open far past the
            # configured ``cooldown_s``, effectively forever.
            if prev is None or now - prev >= self._cooldown:
                self._tripped_at[target] = now
        self._sweep(exclude=target)

    def record_success(self, target: Hashable) -> None:
        """Reset counter and cooldown for a target."""
        self._drop(target)

    def trip(self, target: Hashable) -> None:
        """Open the breaker immediately, skipping the counter."""
        self._counts[target] = self._max
        self._tripped_at[target] = time.time()
        self._half_open.discard(target)
        self._last_touch[target] = time.time()
        # trip() is the dominant leak path (a key tripped once and never
        # revisited), so it must sweep idle keys too — record_failure already
        # does. Without this the never-revisited-key case the leak fix targets
        # would never be pruned.
        self._sweep(exclude=target)

    def reset(self, target: Hashable) -> None:
        """Remove a target from tracking entirely."""
        self._drop(target)

    def reset_all(self) -> None:
        self._counts.clear()
        self._tripped_at.clear()
        self._half_open.clear()
        self._last_touch.clear()

    def _sweep(self, exclude: Hashable | None = None) -> None:
        """Opportunistic prune of long-idle targets (called on every mutation).

        A key whose last failure/trip is older than the cooldown window can no
        longer be open (its window has lapsed), and a sub-threshold counter that
        old is plainly stale. Dropping it is equivalent to what the half-open
        ``is_open`` branch would do if the key were ever polled again, so this
        only removes state the breaker would otherwise discard on next contact —
        but it does so even for keys that are never contacted again, which is
        exactly the unbounded-growth case. ``exclude`` protects the key being
        recorded right now (it was just touched), so re-tripping a lapsed,
        never-polled key (record_failure -> fresh window) is unaffected.
        """
        self.prune_expired(exclude=exclude)

    def prune_expired(self, exclude: Hashable | None = None) -> int:
        """Drop every tracked target idle for longer than the cooldown window.

        Returns the number of targets evicted. Safe to call at any time; never
        evicts a currently-open target (its last touch is within the cooldown)
        nor ``exclude``. Bounds the breaker's memory at the set of distinct keys
        touched within one cooldown window rather than the lifetime key set.
        """
        now = time.time()
        stale = [
            t
            for t, ts in self._last_touch.items()
            if t != exclude and now - ts >= self._cooldown
        ]
        for t in stale:
            self._drop(t)
        return len(stale)

    def _is_open_pure(self, target: Hashable) -> bool:
        """Non-mutating open check used by diagnostic/listing methods.

        Unlike `is_open()`, this never transitions a cooldown-lapsed target
        into the half-open state — so logging a `snapshot()` or scanning
        `open_targets()` can never consume a probe slot or silently clear a
        failure counter as a side effect of a read.
        """
        count = self._counts.get(target, 0)
        if count < self._max:
            return False
        tripped_at = self._tripped_at.get(target, 0.0)
        return time.time() - tripped_at < self._cooldown

    def is_open(self, target: Hashable) -> bool:
        """True while the target is in its cooldown window."""
        count = self._counts.get(target, 0)
        if count < self._max:
            return False
        tripped_at = self._tripped_at.get(target, 0.0)
        if time.time() - tripped_at >= self._cooldown:
            # Cooldown lapsed: clear the counter but remember that this
            # target is now being probed once (half-open). The next
            # failure re-trips it without re-counting to max_failures.
            self._counts.pop(target, None)
            self._tripped_at.pop(target, None)
            self._half_open.add(target)
            return False
        return True

    def failure_count(self, target: Hashable) -> int:
        return self._counts.get(target, 0)

    def is_half_open(self, target: Hashable) -> bool:
        """True if the target's cooldown lapsed and it is being probed.

        Note: a target only transitions to half-open once `is_open()`
        has been evaluated after the cooldown window elapsed.
        """
        return target in self._half_open

    def open_targets(self) -> list[Hashable]:
        """List of targets whose breaker is currently open.

        Pure: never mutates breaker state (no half-open transition).
        """
        return [t for t in list(self._counts.keys()) if self._is_open_pure(t)]

    def snapshot(self) -> dict[str, Any]:
        """Diagnostic snapshot for logging (pure — never mutates state)."""
        now = time.time()
        return {
            "max_failures": self._max,
            "cooldown_s": self._cooldown,
            "tracked": len(self._counts),
            "open": [
                {
                    "target": str(t),
                    "count": self._counts[t],
                    "open_for_more_s": max(
                        0.0, self._cooldown - (now - self._tripped_at.get(t, 0.0))
                    ),
                }
                for t in self.open_targets()
            ],
        }
