"""ActivityFeed must keep a strong reference to fire-and-forget notify tasks.

Regression: ``publish`` used ``loop.create_task(sub(event))`` without retaining
the returned task. The event loop only holds a *weak* reference to a task, so a
subscriber coroutine that suspends at an ``await`` could be garbage-collected
mid-flight and silently never finish. The feed now stores each in-flight task in
``self._tasks`` (dropping it via a done-callback) so the coroutine reliably runs
to completion, and the set is empty again once all subscribers have finished.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from anima.monitor.feed import ActivityEvent, ActivityFeed


@pytest.mark.asyncio
async def test_suspending_subscriber_runs_to_completion_after_gc() -> None:
    """A subscriber that yields at an await still completes even after GC.

    With the buggy fire-and-forget version, forcing a collection while the task
    is suspended could reclaim it before ``done.set()`` ran. With the fix the
    feed holds the only strong reference and the coroutine finishes.
    """
    feed = ActivityFeed(max_events=10)
    done = asyncio.Event()
    seen: list[str] = []

    async def slow_subscriber(event: ActivityEvent) -> None:
        # Suspend so the task is mid-flight (not yet done) when we force GC.
        await asyncio.sleep(0.01)
        seen.append(event.message)
        done.set()

    feed.subscribe(slow_subscriber)
    feed.publish("system", "hello", importance=1)

    # The task must be tracked while in flight (the loop's own reference is weak).
    assert len(feed._tasks) == 1

    # Drop any transient local handles and force a collection while suspended.
    gc.collect()

    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert seen == ["hello"]

    # Let the done-callback fire and verify the task set is cleaned up.
    await asyncio.sleep(0)
    assert feed._tasks == set()


@pytest.mark.asyncio
async def test_multiple_subscribers_all_tracked_then_drained() -> None:
    feed = ActivityFeed(max_events=10)
    counter = {"n": 0}

    async def sub(_event: ActivityEvent) -> None:
        await asyncio.sleep(0)
        counter["n"] += 1

    feed.subscribe(sub)
    feed.subscribe(sub)
    feed.subscribe(sub)

    feed.publish("system", "x", importance=1)
    assert len(feed._tasks) == 3

    # Drain all in-flight tasks.
    await asyncio.gather(*list(feed._tasks))
    await asyncio.sleep(0)

    assert counter["n"] == 3
    assert feed._tasks == set()
