"""A ghost with a stale non-zero health bar must still register as survival-pending.

Priority 0's dead branch gates on the authoritative ``ss.is_alive`` oracle,
which returns False for a GHOST body even when an out-of-order / stale 0xA1
health packet still reports ``hits > 0`` (the body flip is the real death
signal — see ``SelfState.is_alive`` / ``is_ghost``). ``_survival_pending``
used to re-derive "dead" from ``hits <= 0`` alone, so a death that lands during
a 60s planner health break — with the health bar not yet zeroed — read as "not
pending" and the dead ghost was frozen (could not reach seek-resurrection) for
the whole break. These tests pin the oracle-based dead check.
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
        return None

    async def _fake_select(ctx):
        calls["select"] += 1
        return None

    planner._fallback_procedure = _fake_fallback  # type: ignore[assignment]
    planner.select_procedure = _fake_select  # type: ignore[assignment]
    return planner, calls


def _ctx_ghost(*, is_alive: bool, hits: int, hits_max: int = 100):
    """A self_state carrying the authoritative is_alive oracle.

    A ghost reports ``is_alive=False`` while the stale health bar can still
    carry a pre-death ``hits > 0`` (out-of-order 0xA1 after the body flip).
    """
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=SimpleNamespace(
                x=10, y=20, gold=0,
                hits=hits, hits_max=hits_max, is_poisoned=False,
                is_alive=is_alive,
            )
        ),
        blackboard={},
        bus=None,
        memory_db=None,
        persona=None,
    )


def test_ghost_with_stale_nonzero_hp_is_survival_pending():
    """Dead per the oracle, yet hits>0 (stale bar) -> still survival-pending."""
    planner, _ = _make_planner()
    ctx = _ctx_ghost(is_alive=False, hits=42)  # ghost, stale HP not yet zeroed
    assert planner._survival_pending(ctx) is True


def test_alive_full_hp_is_not_survival_pending():
    """Control: alive at full HP via the oracle is NOT pending."""
    planner, _ = _make_planner()
    ctx = _ctx_ghost(is_alive=True, hits=100)
    assert planner._survival_pending(ctx) is False


@pytest.mark.asyncio
async def test_health_break_does_not_freeze_a_freshly_dead_ghost():
    """A ghost (is_alive=False) during a health break must reach select_procedure.

    Regression: the dead ghost's stale ``hits > 0`` made ``_survival_pending``
    return False, so tick() short-circuited for the whole 60s break and the
    ghost never reached Priority 0's seek-resurrection. The oracle-based dead
    check now pierces the break exactly like a wounded/poisoned agent.
    """
    planner, calls = _make_planner()
    ctx = _ctx_ghost(is_alive=False, hits=42)  # dead per oracle, stale bar > 0

    now = _time.time()
    planner._health_break_until = now + 60.0   # health break active
    planner._force_fallback_until = 0.0        # no watchdog window

    await planner.tick(ctx)

    # The break must NOT have short-circuited tick(): survival selection ran.
    assert calls["select"] == 1
    assert calls["fallback"] == 0
