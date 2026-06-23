"""A watchdog-cancelled procedure must count as exactly ONE breaker failure.

The liveness watchdog (``Planner._liveness_watchdog``) cancels a wedged
in-flight procedure and, in the same step, records the stall against the
starvation circuit breaker::

    self._proc_breaker.record_failure(self._last_procedure)

That ``task.cancel()`` raises ``CancelledError`` inside ``tick()``, which catches
it (``aborted = "watchdog_cancelled"``) and produces a *failure* result. The
generic failure-accounting tail of ``tick()`` then used to ALSO call
``self._proc_breaker.record_failure(proc.name)`` — and since
``_last_procedure == proc.name`` for the tick that was cancelled, a single
watchdog stall was charged to the breaker TWICE.

With ``_STARVE_FAILS = 3`` that means a procedure gets demoted (benched for the
whole ``_STARVE_COOLDOWN_S`` window) after only TWO watchdog cancels instead of
three — prematurely starving a slow-but-legitimate procedure (a long combat
leash, a far mining walk) the moment the watchdog twice judged it stalled.

Regression guard: one watchdog-cancel cycle = one breaker failure.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import (
    FailureReason,
    ProcedureRegistry,
    ProcedureResult,
)


def _make_ctx() -> types.SimpleNamespace:
    """A minimal AgentContext stand-in: tick() only needs blackboard, bus, and
    a perception.self_state that reports 'no survival need' (no fields)."""
    # No is_alive/hits → survival not pending. x/y are read by tick()'s
    # action-log tail for non-Procedure helpers, so provide them.
    self_state = types.SimpleNamespace(x=0, y=0)
    perception = types.SimpleNamespace(self_state=self_state)
    return types.SimpleNamespace(
        blackboard={},
        bus=None,
        perception=perception,
        persona=None,
        memory_db=None,
    )


class _WatchdogCancelledProc:
    """Mimics the exact situation the liveness watchdog creates: the planner
    sets ``_watchdog_cancelled = True`` and the running task is cancelled, so
    ``run()`` is interrupted by a CancelledError mid-await."""

    name = "stuck_proc"

    def __init__(self, planner: Planner) -> None:
        self._planner = planner

    async def run(self, ctx) -> ProcedureResult:  # pragma: no cover - cancelled
        # The watchdog flips this flag and cancels the task; reproduce both so
        # tick()'s CancelledError handler takes the watchdog_cancelled branch.
        self._planner._watchdog_cancelled = True
        await asyncio.sleep(3600)
        return ProcedureResult(success=True, message="never reached")


@pytest.mark.asyncio
async def test_watchdog_cancel_records_one_breaker_failure() -> None:
    planner = Planner(ProcedureRegistry())
    ctx = _make_ctx()
    proc = _WatchdogCancelledProc(planner)

    # Route selection straight to the stuck procedure; keep survival inert.
    planner.select_procedure = lambda _ctx: _coro(proc)  # type: ignore[assignment]
    planner._survival_pending = lambda _ctx: False  # type: ignore[assignment]

    # Cancel the in-flight task shortly after tick() awaits it, exactly as the
    # detached watchdog would (which also calls task.cancel()).
    async def _cancel_when_running() -> None:
        for _ in range(200):
            await asyncio.sleep(0.005)
            task = planner._current_proc_task
            if task is not None and not task.done():
                task.cancel()
                return

    # Step 1: the watchdog's OWN accounting fires once when it cancels.
    planner._proc_breaker.record_failure(proc.name)

    # Step 2: tick() runs the proc, the cancel lands, and tick() handles it.
    canceller = asyncio.create_task(_cancel_when_running())
    result = await planner.tick(ctx)
    await canceller

    assert result is not None
    assert not result.success
    assert result.reason == FailureReason.INTERRUPTED

    # The whole point: ONE watchdog stall == ONE breaker failure. tick() must
    # NOT re-record the cancellation the watchdog already charged.
    assert planner._proc_breaker.failure_count(proc.name) == 1


@pytest.mark.asyncio
async def test_ordinary_failure_still_records_a_breaker_failure() -> None:
    """Control: a NORMAL (non-watchdog) procedure failure is still counted, so
    the fix narrows the skip to the watchdog path only."""
    planner = Planner(ProcedureRegistry())
    ctx = _make_ctx()

    class _FailingProc:
        name = "failing_proc"

        async def run(self, _ctx) -> ProcedureResult:
            return ProcedureResult(
                success=False, reason=FailureReason.BLOCKED, message="nope"
            )

    proc = _FailingProc()
    planner.select_procedure = lambda _ctx: _coro(proc)  # type: ignore[assignment]
    planner._survival_pending = lambda _ctx: False  # type: ignore[assignment]

    await planner.tick(ctx)

    assert planner._proc_breaker.failure_count(proc.name) == 1


async def _coro(value):
    """Wrap a value so a lambda can stand in for an async method."""
    return value
