"""MetaController.close() must cancel its detached background decision task.

``maybe_decide`` spawns ``_do_decide`` via a bare ``asyncio.create_task`` so a
slow LLM never stalls the planner tick. That task is NOT a child of the
planner's ``TaskGroup``, so when the session ends (disconnect / eval-window
close / reconnect) it is orphaned: it keeps the ``ctx``/``llm`` alive, may emit
a stray LLM call or shadow-log write after teardown, and trips asyncio's
"Task was destroyed but it is pending" warning.

Regression guard: ``close()`` cancels and awaits the in-flight task, is a no-op
when nothing is in flight, and never raises.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.planner.meta_controller import MetaController


@pytest.mark.asyncio
async def test_close_cancels_in_flight_decide_task():
    c = MetaController()

    started = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        # Stand in for a slow LLM call suspended inside _do_decide.
        await asyncio.sleep(3600)

    c._decide_task = asyncio.create_task(_never_finishes())
    await started.wait()  # ensure the task is actually suspended mid-await
    assert not c._decide_task.done()

    await c.close()

    # The orphaned task is cancelled (not left running) and the handle cleared.
    assert c._decide_task is None


@pytest.mark.asyncio
async def test_close_is_noop_when_no_task_in_flight():
    c = MetaController()
    assert c._decide_task is None
    # Must not raise when there is nothing to reap.
    await c.close()
    await c.close()  # idempotent
    assert c._decide_task is None


@pytest.mark.asyncio
async def test_close_swallows_already_finished_task():
    c = MetaController()

    async def _quick() -> None:
        return None

    c._decide_task = asyncio.create_task(_quick())
    await c._decide_task  # let it finish
    assert c._decide_task.done()

    # Already-done task: close() clears the handle without error.
    await c.close()
    assert c._decide_task is None


@pytest.mark.asyncio
async def test_no_pending_task_warning_after_close():
    """The whole point of close(): the task is reaped, not left pending. After
    close() there is no live reference and the underlying task is in a terminal
    (cancelled) state, so the loop has nothing to destroy at shutdown."""
    c = MetaController()

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never_finishes())
    c._decide_task = task
    # Yield so the task starts running and suspends.
    await asyncio.sleep(0)

    await c.close()

    assert task.cancelled()
    assert task.done()
