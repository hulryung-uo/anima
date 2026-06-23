"""A frozen OVERRIDE procedure must be cancellable by the liveness watchdog.

``_handle_overrides`` (dashboard ``go_to`` / ``run_procedure`` steering) used to
call ``proc.run(ctx)`` directly, leaving ``self._current_proc_task`` untouched.
``_liveness_watchdog`` reads ``self._current_proc_task`` and calls
``task.cancel()`` on a stalled procedure — so an override that froze (a
``_MoveToProcedure`` pathing to an unreachable tile, or a forced procedure
awaiting a server packet that never comes) was INVISIBLE to the watchdog: the
slot was ``None`` (or a stale, already-done task from an earlier ``tick()``), the
watchdog's ``task.cancel()`` never reached the running override, and the planner
loop wedged with no anti-freeze cover at all.

Regression guard: ``_run_tracked`` (which both override branches now use)

  1. registers the running procedure as ``self._current_proc_task`` while it
     runs (so the watchdog can see and cancel it), and
  2. turns a watchdog-flagged cancel into an INTERRUPTED ProcedureResult rather
     than letting the CancelledError escape, while still propagating a real
     (non-watchdog) shutdown cancel.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
)


class _FrozenProc(Procedure):
    """A procedure that never completes on its own — stands in for a freeze."""

    name = "frozen_override"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def can_start(self, ctx) -> bool:  # type: ignore[override]
        return True

    async def execute(self, ctx) -> ProcedureResult:  # type: ignore[override]
        return ProcedureResult(success=True)  # pragma: no cover

    async def run(self, ctx) -> ProcedureResult:  # type: ignore[override]
        # Override run() directly so the freeze does not depend on ctx-driven
        # action-log writes (_run_tracked wraps run(), which is the contract
        # the watchdog cancels through).
        self.started.set()
        await asyncio.sleep(3600)  # block forever, like an unreachable-tile path
        return ProcedureResult(success=True)  # pragma: no cover


@pytest.fixture
def planner() -> Planner:
    return Planner(ProcedureRegistry())


@pytest.mark.asyncio
async def test_running_override_is_tracked_and_watchdog_cancellable(
    planner: Planner,
) -> None:
    proc = _FrozenProc()

    runner = asyncio.create_task(planner._run_tracked(proc, ctx=None))
    await proc.started.wait()  # override is genuinely running and suspended

    # (1) Visibility: the watchdog reads exactly this attribute.
    task = planner._current_proc_task
    assert task is not None and not task.done()

    # (2) Drive the watchdog's cancel path: flag + cancel the tracked task.
    planner._watchdog_cancelled = True
    task.cancel()

    result = await runner

    # The frozen override unwinds into an INTERRUPTED result — not a propagated
    # CancelledError that would tear down the planner loop.
    assert isinstance(result, ProcedureResult)
    assert result.success is False
    assert result.reason is FailureReason.INTERRUPTED

    # Slot is cleared so a later watchdog poll can't cancel a stale task.
    assert planner._current_proc_task is None


@pytest.mark.asyncio
async def test_real_shutdown_cancel_still_propagates(planner: Planner) -> None:
    proc = _FrozenProc()

    runner = asyncio.create_task(planner._run_tracked(proc, ctx=None))
    await proc.started.wait()

    # NOT a watchdog cancel (flag stays False) — a real shutdown must propagate
    # rather than be swallowed into an INTERRUPTED result.
    assert planner._watchdog_cancelled is False
    planner._current_proc_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await runner

    assert planner._current_proc_task is None
