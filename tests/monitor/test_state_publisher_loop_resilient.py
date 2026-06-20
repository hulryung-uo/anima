"""StatePublisher.run() must survive a transient publish/serialize glitch.

The publisher loop is a sibling coroutine in the game-session asyncio.TaskGroup
(anima/main.py). A TaskGroup cancels ALL siblings the moment one raises, so an
unguarded exception in this purely-cosmetic 0.5s loop would tear down recv_loop,
the planner and the API server and force a full reconnect — losing the agent's
in-world progress (and corrupting a Foundry eval window). The loop must instead
swallow a transient fault, log it, and keep ticking.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.core.bus import EventBus
from anima.monitor.state_publisher import StatePublisher


def _make_publisher(bus: EventBus) -> StatePublisher:
    # The constructor only needs a real bus (it subscribes for activity);
    # publish_all / _dump_to_file are overridden below so the perception and
    # blackboard collaborators are never touched by the loop under test.
    return StatePublisher(
        perception=None,  # type: ignore[arg-type]
        blackboard={},
        bus=bus,
    )


@pytest.mark.asyncio
async def test_run_loop_survives_a_transient_publish_error() -> None:
    bus = EventBus()
    pub = _make_publisher(bus)

    calls = {"publish": 0, "dump": 0}

    def boom_once() -> None:
        calls["publish"] += 1
        # Raise on the FIRST tick only; subsequent ticks succeed. A loop that
        # propagated the exception would never reach the second tick.
        if calls["publish"] == 1:
            raise RuntimeError("transient perception glitch")

    def count_dump() -> None:
        calls["dump"] += 1

    pub.publish_all = boom_once  # type: ignore[method-assign]
    pub._dump_to_file = count_dump  # type: ignore[method-assign]

    task = asyncio.create_task(pub.run(interval=0.001))
    try:
        # Give the loop enough wall-clock to tick several times across the
        # raising first iteration and the recovering ones.
        for _ in range(200):
            await asyncio.sleep(0.001)
            if calls["publish"] >= 3 and calls["dump"] >= 1:
                break
        # The loop kept running past the exception (>=2 publish attempts) AND
        # recovered far enough to reach _dump_to_file on a healthy tick.
        assert calls["publish"] >= 2, (
            "run() stopped after the first raising tick — the loop is unguarded "
            "and would tear down the whole game-session TaskGroup"
        )
        assert calls["dump"] >= 1, "loop never recovered to a healthy dump tick"
        # The task is still alive and was not killed by the exception.
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_loop_still_propagates_cancellation() -> None:
    """A real shutdown (CancelledError) must still tear the loop down cleanly."""
    bus = EventBus()
    pub = _make_publisher(bus)
    pub.publish_all = lambda: None  # type: ignore[method-assign]
    pub._dump_to_file = lambda: None  # type: ignore[method-assign]

    task = asyncio.create_task(pub.run(interval=0.001))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
