"""Idle/stuck escalation must fire even when the idle counter skips a threshold.

_check_stuck() escalates a stalled planner through three rungs: a warning at
_IDLE_WARN, the deadlock resolver at _IDLE_ESCALATE, and forum escalation at
_IDLE_FORUM. _idle_ticks is incremented once per loop in _run_loop one step
before _check_stuck runs, but a tick can advance the counter WITHOUT
_check_stuck observing that exact value:

  * the health-break early-return at the top of _check_stuck skips a tick, and
  * any exception earlier in the loop body skips the _check_stuck call while
    the increment already happened.

The original code matched thresholds with `==`, so a single skipped
observation at the threshold value meant the rung NEVER fired and the agent
sat idle for the rest of the session. These tests lock in crossing detection.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _make_planner() -> tuple[Planner, dict]:
    reg = ProcedureRegistry()
    planner = Planner(reg)
    calls: dict[str, int] = {"resolve": 0, "forum": 0}

    async def _fake_resolve(ctx):
        calls["resolve"] += 1

    async def _fake_forum(ctx):
        calls["forum"] += 1

    planner._deadlock.resolve = _fake_resolve  # type: ignore[assignment]
    planner._deadlock.escalate_to_forum = _fake_forum  # type: ignore[assignment]
    return planner, calls


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=SimpleNamespace(x=10, y=20, gold=0)),
        blackboard={},
        bus=None,
    )


@pytest.mark.asyncio
async def test_escalate_fires_when_counter_overshoots_threshold():
    """A counter that jumps PAST _IDLE_ESCALATE must still run the resolver."""
    planner, calls = _make_planner()
    ctx = _ctx()

    # The tick on which the counter would equal _IDLE_ESCALATE was skipped
    # (e.g. a health-break early-return), so _check_stuck first sees a value
    # one past it. The old `==` test would miss this forever.
    planner._idle_ticks = planner._IDLE_ESCALATE + 1
    await planner._check_stuck(ctx)

    assert calls["resolve"] == 1
    assert calls["forum"] == 0


@pytest.mark.asyncio
async def test_forum_fires_when_counter_overshoots_threshold():
    """A counter that jumps PAST _IDLE_FORUM must still escalate to forum."""
    planner, calls = _make_planner()
    ctx = _ctx()

    planner._idle_ticks = planner._IDLE_FORUM + 5
    await planner._check_stuck(ctx)

    assert calls["forum"] == 1


@pytest.mark.asyncio
async def test_each_rung_fires_at_most_once_per_idle_run():
    """Holding above a threshold across many ticks fires the rung only once."""
    planner, calls = _make_planner()
    ctx = _ctx()

    planner._idle_ticks = planner._IDLE_ESCALATE
    for _ in range(5):
        await planner._check_stuck(ctx)
        planner._idle_ticks += 1  # still idle, climbing

    # Resolver fired once on the crossing, not once per tick.
    assert calls["resolve"] == 1
    # Forum not reached yet.
    assert calls["forum"] == 0


@pytest.mark.asyncio
async def test_counter_reset_rearms_escalation():
    """After a procedure runs (counter resets), the rungs re-arm."""
    planner, calls = _make_planner()
    ctx = _ctx()

    planner._idle_ticks = planner._IDLE_ESCALATE
    await planner._check_stuck(ctx)
    assert calls["resolve"] == 1

    # A procedure ran: _run_loop sets _idle_ticks = 0.
    planner._idle_ticks = 0
    await planner._check_stuck(ctx)  # re-arms the watermark

    # New idle run climbs back to the resolve threshold — must fire again.
    planner._idle_ticks = planner._IDLE_ESCALATE
    await planner._check_stuck(ctx)
    assert calls["resolve"] == 2


@pytest.mark.asyncio
async def test_health_break_suppresses_escalation():
    """During a health break _check_stuck must not escalate."""
    import time as _time

    planner, calls = _make_planner()
    ctx = _ctx()

    planner._health_break_until = _time.time() + 60.0
    planner._idle_ticks = planner._IDLE_FORUM + 1
    await planner._check_stuck(ctx)

    assert calls["resolve"] == 0
    assert calls["forum"] == 0
