"""The meta-controller decision cadence must run on a MONOTONIC clock.

Regression guard for the wall-clock cadence bug. ``should_decide`` used
``time.time()`` (wall clock) for the "seconds since last decision" throttle.
Wall time is not monotonic: an NTP correction or a VM resume can step it
BACKWARD by minutes, making ``now - last_decide`` go negative — so the gate
stays closed for the whole backward step and the P0 shadow-decision log
silently freezes. ``StrategySelector.should_refresh`` was already fixed to use
``time.monotonic()`` (commit c1bef3d); its docstring explicitly named THIS
controller as still carrying the wall-clock pattern. These tests pin the fix.
"""

from __future__ import annotations

import time

import anima.planner.meta_controller as mc
from anima.planner.meta_controller import MetaController


def test_first_decision_is_always_eligible():
    # ``None`` means "never decided" → the very first tick must be eligible,
    # regardless of the monotonic origin (which can be a small seconds-since-
    # boot value that ``now - 0.0`` would have falsely suppressed).
    c = MetaController(interval_s=180)
    assert c._last_decide is None
    assert c.should_decide() is True


def test_cadence_uses_monotonic_not_wall_clock(monkeypatch):
    """A wall-clock step backward must NOT freeze decisions.

    Drive ``should_decide`` off a controllable monotonic clock and prove the
    interval is honoured against THAT clock, not ``time.time()``.
    """
    fake_mono = {"t": 1000.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: fake_mono["t"])

    c = MetaController(interval_s=180)
    # First decision spawns and stamps the monotonic clock.
    c._last_decide = mc.time.monotonic()

    # Step the WALL clock backward by an hour. If the gate read wall time it
    # would now compute a hugely negative ``now - last`` and stay closed even
    # once the real interval elapses.
    orig_time = mc.time.time
    monkeypatch.setattr(mc.time, "time", lambda: orig_time() - 3600.0)

    # Not enough monotonic time has passed yet → still throttled.
    fake_mono["t"] = 1000.0 + 179.0
    assert c.should_decide() is False

    # Once the monotonic interval elapses, the gate opens — unaffected by the
    # backward wall-clock step.
    fake_mono["t"] = 1000.0 + 181.0
    assert c.should_decide() is True


def test_explicit_now_arg_is_compared_against_monotonic_last(monkeypatch):
    # When a caller passes ``now`` explicitly it is compared against the
    # monotonic ``_last_decide`` stamp, so the throttle is consistent.
    fake_mono = {"t": 500.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: fake_mono["t"])
    c = MetaController(interval_s=60)
    c._last_decide = mc.time.monotonic()  # stamped at 500.0
    assert c.should_decide(now=500.0 + 59.0) is False
    assert c.should_decide(now=500.0 + 61.0) is True


def test_maybe_decide_stamps_monotonic_last_decide(monkeypatch):
    """The spawn path stamps ``_last_decide`` from the monotonic clock so the
    subsequent ``should_decide`` comparison stays on one clock."""
    import asyncio
    from unittest.mock import MagicMock

    fake_mono = {"t": 2000.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: fake_mono["t"])

    c = MetaController(interval_s=60)
    ctx = MagicMock()
    ctx.llm = None
    ctx.persona.profession = "miner"
    ss = ctx.perception.self_state
    ss.hits = 80; ss.hits_max = 100; ss.weight = 50; ss.weight_max = 400
    ss.gold = 100; ss.x = 10; ss.y = 20; ss.serial = 0x1
    ss.equipment = {0x15: 0x101}
    ctx.blackboard = {}
    ctx.perception.world.items = {}
    ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])

    async def _drive() -> None:
        spawned = await c.maybe_decide(ctx)
        assert spawned is True
        await c._decide_task

    asyncio.run(_drive())
    # _last_decide was stamped from the (fake) monotonic clock, not wall time.
    assert c._last_decide == 2000.0
    # Immediately after, the interval gate is closed on the monotonic clock.
    assert c.should_decide() is False
