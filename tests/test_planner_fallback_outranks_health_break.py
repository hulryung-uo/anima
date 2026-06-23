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


def _ctx_hp(hits: int, hits_max: int = 100, poisoned: bool = False):
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=SimpleNamespace(
                x=10, y=20, gold=0,
                hits=hits, hits_max=hits_max, is_poisoned=poisoned,
            )
        ),
        blackboard={},
        bus=None,
        memory_db=None,
        persona=None,
    )


def test_survival_pending_detects_wounded_dead_and_poisoned():
    planner, _ = _make_planner()
    # Healthy, not poisoned -> no survival need.
    assert planner._survival_pending(_ctx_hp(hits=100)) is False
    # Wounded below the 40% heal-in-place floor -> survival need.
    assert planner._survival_pending(_ctx_hp(hits=30)) is True
    # Dead (hits <= 0) -> survival need.
    assert planner._survival_pending(_ctx_hp(hits=0)) is True
    # Poisoned at full HP -> survival need (poison is invisible to HP gates).
    assert planner._survival_pending(_ctx_hp(hits=100, poisoned=True)) is True
    # A minimal self_state (no hits fields) must not raise -> no survival need.
    assert planner._survival_pending(_ctx()) is False


@pytest.mark.asyncio
async def test_health_break_does_not_suppress_survival():
    """A wounded agent during a health break must still reach select_procedure.

    Regression: the survival ladder (flee/heal/cure/resurrect) lives inside
    select_procedure, but tick() returned None for the whole 60s health break
    without calling it. A non-productive loop that tripped a break therefore
    left a wounded or poisoned agent unable to defend itself and bleeding out.
    Survival now pierces the break the same way the forced fallback does.
    """
    planner, calls = _make_planner()
    ctx = _ctx_hp(hits=25)  # below the 40% heal-in-place floor

    now = _time.time()
    planner._health_break_until = now + 60.0   # health break active
    planner._force_fallback_until = 0.0        # no watchdog window

    await planner.tick(ctx)

    # The break must NOT have short-circuited tick(): survival selection ran.
    assert calls["select"] == 1
    # And the forced-fallback path was never used (no watchdog window).
    assert calls["fallback"] == 0


@pytest.mark.asyncio
async def test_health_break_still_pauses_a_poisoned_then_cured_agent():
    """Healthy + un-poisoned during a plain health break still pauses (control)."""
    planner, calls = _make_planner()
    ctx = _ctx_hp(hits=100, poisoned=False)

    now = _time.time()
    planner._health_break_until = now + 60.0
    planner._force_fallback_until = 0.0

    result = await planner.tick(ctx)

    assert result is None
    assert calls["select"] == 0


@pytest.mark.asyncio
async def test_forced_fallback_yields_to_survival():
    """A wounded agent inside a forced-fallback window must reach the survival
    ladder, NOT the liveness-only fallback.

    Regression: the liveness watchdog opens a 60s forced-fallback window after
    _WATCHDOG_STALL_S of no positional progress. A wounded/poisoned agent pinned
    by a swarm cannot move, so its (x,y) is static — the exact no-progress
    signal that opens the window. tick() used to run _fallback_procedure
    (mine_ore/chop_wood/wander) for the whole window, bypassing the
    flee/heal/cure ladder inside select_procedure, so the dying agent walked off
    to "mine" while the swarm killed it. Survival must pierce the forced
    fallback the same way it pierces the health break.
    """
    planner, calls = _make_planner()
    ctx = _ctx_hp(hits=25)  # below the 40% heal-in-place floor → survival pending

    now = _time.time()
    planner._force_fallback_until = now + 30.0  # watchdog window open
    planner._health_break_until = 0.0

    await planner.tick(ctx)

    # The survival ladder ran...
    assert calls["select"] == 1
    # ...and the liveness-only fallback was NOT used while the agent was dying.
    assert calls["fallback"] == 0


@pytest.mark.asyncio
async def test_forced_fallback_still_used_when_healthy():
    """Control: a healthy agent inside a forced-fallback window still gets the
    liveness-only fallback (the watchdog's freeze-recovery is preserved)."""
    planner, calls = _make_planner()
    ctx = _ctx_hp(hits=100, poisoned=False)  # no survival need

    now = _time.time()
    planner._force_fallback_until = now + 30.0
    planner._health_break_until = 0.0

    await planner.tick(ctx)

    # Fallback was tried (returns None in the stub) and then select ran.
    assert calls["fallback"] == 1
    assert calls["select"] == 1
