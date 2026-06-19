"""Tests for ActivityFeed ring buffer and read surface."""

from __future__ import annotations

from anima.monitor.feed import ActivityFeed


def _fill(feed: ActivityFeed, n: int) -> None:
    for i in range(n):
        feed.publish("system", f"event-{i}", importance=1)


def test_recent_zero_returns_empty_not_whole_buffer() -> None:
    """recent(0) must return no events.

    Regression: `events[-0:]` is `events[0:]`, so count==0 used to leak the
    entire ring buffer instead of an empty slice.
    """
    feed = ActivityFeed(max_events=50)
    _fill(feed, 10)
    assert feed.recent(0) == []


def test_recent_negative_returns_empty() -> None:
    feed = ActivityFeed(max_events=50)
    _fill(feed, 10)
    assert feed.recent(-5) == []


def test_recent_returns_tail_in_order() -> None:
    feed = ActivityFeed(max_events=50)
    _fill(feed, 10)
    msgs = [ev.message for ev in feed.recent(3)]
    assert msgs == ["event-7", "event-8", "event-9"]


def test_recent_count_exceeds_size() -> None:
    feed = ActivityFeed(max_events=50)
    _fill(feed, 3)
    assert len(feed.recent(20)) == 3


def test_ring_buffer_bounded_by_max_events() -> None:
    feed = ActivityFeed(max_events=5)
    _fill(feed, 12)
    assert feed.total_count == 5
    msgs = [ev.message for ev in feed.recent(100)]
    assert msgs == ["event-7", "event-8", "event-9", "event-10", "event-11"]
