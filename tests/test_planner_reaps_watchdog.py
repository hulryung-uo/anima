"""Planner.run() must cancel AND await its detached liveness-watchdog task.

``_liveness_watchdog`` is spawned with a bare ``asyncio.create_task`` (not a
TaskGroup child) so it can fire while ``tick()`` is blocked inside a frozen
procedure. ``run()``'s ``finally`` used to call ``watchdog_task.cancel()`` and
return WITHOUT awaiting it. ``cancel()`` only *schedules* the CancelledError, so
the watchdog was still pending when ``run()`` returned — its CancelledError was
never retrieved and, because ``run()`` is a child of the game-session
``TaskGroup`` (main.py), asyncio logs "Task was destroyed but it is pending" at
loop teardown. The watchdog's own CancelledError-only ``except`` (which
re-raises for a clean shutdown) likewise never completed.

Regression guard: ``_reap_watchdog`` cancels and awaits the in-flight task,
driving it to a terminal (cancelled) state; is a no-op for a missing or
already-done task; and never raises.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


@pytest.fixture
def planner() -> Planner:
    return Planner(ProcedureRegistry())


@pytest.mark.asyncio
async def test_reap_cancels_and_awaits_in_flight_watchdog(planner: Planner) -> None:
    started = asyncio.Event()

    async def _watchdog_like() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)  # stand in for the watchdog's poll loop
        except asyncio.CancelledError:
            # Mirror _liveness_watchdog: a real shutdown re-raises so the task
            # actually unwinds rather than swallowing the cancel.
            raise

    task = asyncio.create_task(_watchdog_like())
    await started.wait()  # ensure it is genuinely suspended mid-await
    assert not task.done()

    await planner._reap_watchdog(task)

    # The whole point: after reaping the task is in a terminal cancelled state,
    # not merely "cancel scheduled" — so the loop has nothing pending to destroy.
    assert task.cancelled()
    assert task.done()


@pytest.mark.asyncio
async def test_reap_is_noop_for_none(planner: Planner) -> None:
    # Must not raise when there is nothing to reap.
    await planner._reap_watchdog(None)


@pytest.mark.asyncio
async def test_reap_swallows_already_finished_task(planner: Planner) -> None:
    async def _quick() -> None:
        return None

    task = asyncio.create_task(_quick())
    await task  # let it finish normally
    assert task.done() and not task.cancelled()

    # Already-done task: reap returns without error and without flipping state.
    await planner._reap_watchdog(task)
    assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_no_pending_task_after_reap(planner: Planner) -> None:
    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never_finishes())
    await asyncio.sleep(0)  # let it start and suspend

    await planner._reap_watchdog(task)

    # Terminal + retrieved: nothing left for the event loop to warn about.
    assert task.cancelled()
    assert task.done()
