"""planner_loop must REAP its forum background task on shutdown.

The forum loop runs as a bare ``asyncio.create_task`` and spends most of its
life suspended mid-``await`` on an HTTP post / LLM call. On planner exit
(disconnect, eval-window close, reconnect) a bare ``forum_task.cancel()`` only
*requests* cancellation and returns immediately, leaving the task pending and
detached while the reconnect loop tears down the connection/LLM underneath it.
``_reap_task`` must cancel AND await the task so it is fully unwound — and
swallow the resulting CancelledError so nothing escapes.
"""

from __future__ import annotations

import asyncio

from anima.main import _reap_task


async def test_reap_task_awaits_cancellation_to_done() -> None:
    started = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        # Simulate the forum loop suspended on a long network/LLM await.
        await asyncio.sleep(3600)

    task = asyncio.create_task(_long_running())
    await started.wait()  # ensure the task is actually suspended in an await
    assert not task.done()

    await _reap_task(task)

    # The task is fully terminal (not merely cancel-requested-and-pending),
    # and the CancelledError was retrieved inside _reap_task — not left to
    # surface as an "exception was never retrieved" warning.
    assert task.done()
    assert task.cancelled()


async def test_reap_task_noop_on_already_done() -> None:
    async def _quick() -> int:
        return 42

    task = asyncio.create_task(_quick())
    await task  # let it finish normally
    assert task.done()

    # Reaping an already-finished task must be a harmless no-op (it must not
    # cancel/clobber a task that completed successfully).
    await _reap_task(task)
    assert task.result() == 42
    assert not task.cancelled()


async def test_reap_task_swallows_task_exception() -> None:
    started = asyncio.Event()

    async def _boom() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            # On cancellation, raise a *different* exception during unwind to
            # prove _reap_task surfaces nothing to the caller.
            raise RuntimeError("teardown blew up")

    task = asyncio.create_task(_boom())
    await started.wait()

    # Must not raise despite the task's finally raising a non-CancelledError.
    await _reap_task(task)
    assert task.done()
