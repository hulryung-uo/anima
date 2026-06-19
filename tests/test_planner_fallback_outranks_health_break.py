"""A forced-fallback window must outrank the planner health break.

The liveness watchdog opens a forced-fallback window (``_force_fallback_until``)
only after ``_WATCHDOG_STALL_S`` of no progress — it is the planner's last line
of defense against a freeze, guaranteeing a wire action so the kernel's liveness
gate keeps measuring the agent as alive.

A 60s health break (``_health_break_until``, set when a non-productive loop is
detected) is itself a deliberate no-progress stall. If the watchdog fires during
that break and tick() checked the health break FIRST, tick() would return None
for the rest of the break — swallowing the forced fallback and re-freezing the
very agent the watchdog just tried to free. These tests lock in the precedence:
the forced fallback wins, and a plain health break (no fallback) still pauses.
"""

from __future__ import annotations

import time as _time
from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _make_planner() -> tuple[Planner, dict]:
    reg = ProcedureRegistry()
    planner = Planner(reg)
    calls: dict[str, int] = {"fallback": 0, "select": 0}

    async def _fake_fallback(ctx):
        calls["fallback"] += 1
        return None  # keep tick() side-effect free (no proc run / DB write)

    async def _fake_select(ctx):
        calls["select"] += 1
        return None

    planner._fallback_procedure = _fake_fallback  # type: ignore[assignment]
    planner.select_procedure = _fake_select  # type: ignore[assignment]
    return planner, calls


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=SimpleNamespace(x=10, y=20, gold=0)),
        blackboard={},
        bus=None,
        memory_db=None,
        persona=None,
    )


@pytest.mark.asyncio
async def test_forced_fallback_overrides_active_health_break():
    """Watchdog fired (fallback window open) during a health break -> still acts."""
    planner, calls = _make_planner()
    ctx = _ctx()

    now = _time.time()
    planner._health_break_until = now + 60.0   # health break active
    planner._force_fallback_until = now + 30.0  # watchdog forced a fallback

    await planner.tick(ctx)

    # The fallback must run despite the health break being active...
    assert calls["fallback"] == 1
    # ...and the health-break early-return must not have short-circuited tick().
    assert calls["select"] == 1


@pytest.mark.asyncio
async def test_health_break_without_fallback_still_pauses():
    """A plain health break (no forced-fallback window) still returns None."""
    planner, calls = _make_planner()
    ctx = _ctx()

    now = _time.time()
    planner._health_break_until = now + 60.0
    planner._force_fallback_until = 0.0  # no watchdog window

    result = await planner.tick(ctx)

    assert result is None
    assert calls["fallback"] == 0
    assert calls["select"] == 0
