"""The liveness watchdog must survive a transient exception in its body.

The watchdog runs as a detached, never-awaited task (asyncio.create_task in
Planner.run). It is the planner's last line of defense against a freeze: it
cancels stalled procedures and forces fallback activity. If an unhandled
exception escaped one iteration, the task would die silently and the agent
could stall undetected for the rest of the eval window — destroying the
liveness/fitness signal the kernel measures. This locks in the per-iteration
guard.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


class _FlakyConn:
    """connected stays True for a few polls, then flips False to end the loop."""

    def __init__(self, true_polls: int) -> None:
        self._left = true_polls

    @property
    def connected(self) -> bool:
        if self._left > 0:
            self._left -= 1
            return True
        return False


class _ExplodingPerception:
    """Every read of self_state raises — simulates a transient glitch."""

    calls = 0

    @property
    def self_state(self):
        type(self).calls += 1
        raise RuntimeError("transient perception glitch")


@pytest.mark.asyncio
async def test_watchdog_survives_body_exception(monkeypatch):
    # Don't actually sleep 5s between polls.
    monkeypatch.setattr(Planner, "_WATCHDOG_POLL_S", 0.0)

    reg = ProcedureRegistry()
    planner = Planner(reg)
    planner._running = True

    perception = _ExplodingPerception()
    ctx = SimpleNamespace(
        conn=_FlakyConn(true_polls=3),
        perception=perception,
        command_bus=None,
        bus=None,
    )
    planner.command_bus = None

    # Must NOT raise even though the body throws on every iteration.
    await planner._liveness_watchdog(ctx)

    # The body actually ran (and threw) on each connected poll, proving the
    # guard — not an early loop exit — is what kept the task alive.
    assert perception.calls == 3


@pytest.mark.asyncio
async def test_watchdog_propagates_cancellation(monkeypatch):
    """A real shutdown (CancelledError) must still propagate, not be swallowed."""
    import asyncio

    monkeypatch.setattr(Planner, "_WATCHDOG_POLL_S", 0.0)

    reg = ProcedureRegistry()
    planner = Planner(reg)
    planner._running = True

    class _CancellingPerception:
        @property
        def self_state(self):
            raise asyncio.CancelledError()

    ctx = SimpleNamespace(
        conn=_FlakyConn(true_polls=5),
        perception=_CancellingPerception(),
        command_bus=None,
        bus=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await planner._liveness_watchdog(ctx)
