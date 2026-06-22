"""Tests for anima.monitor.metrics.MetricsCollector (WindowMetrics)."""
from __future__ import annotations

from anima.monitor.metrics import MetricsCollector


def test_distance_moved_is_idempotent_across_repeated_get_window() -> None:
    """Calling get_window() repeatedly must yield the SAME distance_moved.

    distance_moved is a per-window metric; the position cursor used to compute
    it must not leak between calls. Regression guard for the case where the
    first in-window step diffed against a stale cursor from a prior call.
    """
    col = MetricsCollector(window_seconds=600.0)
    col.record("walk_confirmed", {"pos": (10, 10)})
    col.record("walk_confirmed", {"pos": (11, 10)})
    col.record("walk_confirmed", {"pos": (12, 10)})

    first = col.get_window().distance_moved
    second = col.get_window().distance_moved
    third = col.get_window().distance_moved

    # 3 confirmed steps at distinct positions => 2 transitions (first has no
    # predecessor inside the window).
    assert first == 2
    assert first == second == third


def test_distance_moved_counts_only_position_changes() -> None:
    col = MetricsCollector(window_seconds=600.0)
    col.record("walk_confirmed", {"pos": (5, 5)})
    col.record("walk_confirmed", {"pos": (5, 5)})  # same tile, no move
    col.record("walk_confirmed", {"pos": (6, 5)})

    m = col.get_window()
    assert m.distance_moved == 1
    assert len(m.unique_positions) == 2
    assert m.walk_confirmed == 3


def test_first_in_window_step_has_no_phantom_distance() -> None:
    """A single confirmed step cannot register movement against a None cursor."""
    col = MetricsCollector(window_seconds=600.0)
    col.record("walk_confirmed", {"pos": (1, 1)})
    assert col.get_window().distance_moved == 0
    # And still zero on a second read (no leaked cursor).
    assert col.get_window().distance_moved == 0
