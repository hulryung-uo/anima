"""_MoveToProcedure must yield the walk back to the survival ladder at the
SAME HP floor that ladder fires on.

The planner's survival block flees (_FLEE_HP_PCT) and heals-in-place
(_HEAL_IN_PLACE_HP_PCT) at 0.40 of max HP. A long _MoveToProcedure walk
(timeout 600s) interrupts itself via go_to's interrupt_check; if that check
used a lower floor than the survival ladder, a wounded agent in the 30–40%
band kept walking — unable to flee or heal — until HP fell below the move's
floor or the destination was reached. This is the move-to analogue of the
heal-in-place 30–40% dead zone closed in commit b24d9cb. The interrupt floor
must match _HEAL_IN_PLACE_HP_PCT so the move yields the moment survival should
take over.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anima.action.movement as movement
from anima.planner.helpers import _MoveToProcedure
from anima.planner.planner import _HEAL_IN_PLACE_HP_PCT


def _ctx(hits: int, hits_max: int = 100):
    ss = SimpleNamespace(x=10, y=10, hits=hits, hits_max=hits_max)
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


def _run_capture(monkeypatch, ctx):
    """Run _MoveToProcedure.run with go_to stubbed, returning the
    interrupt_check it was handed."""
    captured: dict = {}

    async def fake_go_to(ctx, x, y, interrupt_check=None, **kw):
        captured["interrupt_check"] = interrupt_check
        # Report the interrupt verdict at the moment of the (single) check.
        captured["interrupted"] = bool(interrupt_check and interrupt_check())
        return False  # never "arrive" — we only care about the interrupt

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    proc = _MoveToProcedure("forge", 50, 50)
    asyncio.run(proc.run(ctx))
    return captured


def test_interrupt_fires_in_30_to_40_band(monkeypatch):
    """At 35% HP (inside the old dead zone) the move must interrupt so the
    planner re-enters select_procedure and can flee/heal."""
    cap = _run_capture(monkeypatch, _ctx(hits=35))
    assert cap["interrupt_check"] is not None
    assert cap["interrupted"] is True


def test_interrupt_floor_matches_survival_floor(monkeypatch):
    """The interrupt floor must equal the survival ladder floor: just BELOW it
    interrupts, just ABOVE it does not."""
    floor = _HEAL_IN_PLACE_HP_PCT  # 0.40
    just_below = int(floor * 100) - 1   # 39% HP -> wounded enough to yield
    just_above = int(floor * 100) + 1   # 41% HP -> keep walking

    cap_below = _run_capture(monkeypatch, _ctx(hits=just_below))
    assert cap_below["interrupted"] is True

    cap_above = _run_capture(monkeypatch, _ctx(hits=just_above))
    assert cap_above["interrupted"] is False


def test_old_threshold_would_have_missed_the_band(monkeypatch):
    """Document the bug: the old hardcoded 0.30 floor did NOT interrupt at 35%
    HP, so the move kept walking through the survival band."""
    ctx = _ctx(hits=35)
    old_floor = 0.30
    ss = ctx.perception.self_state
    old_verdict = ss.hits_max > 0 and ss.hits < ss.hits_max * old_floor
    assert old_verdict is False  # the leak: no interrupt at 35% under the old code

    # The shipped floor closes it.
    new_verdict = ss.hits_max > 0 and ss.hits < ss.hits_max * _HEAL_IN_PLACE_HP_PCT
    assert new_verdict is True


def test_full_health_never_interrupts(monkeypatch):
    """A healthy agent walks uninterrupted."""
    cap = _run_capture(monkeypatch, _ctx(hits=100))
    assert cap["interrupted"] is False


def test_zero_max_hp_does_not_interrupt(monkeypatch):
    """A degenerate hits_max=0 (early boot / partial state) must not divide-by
    or spuriously interrupt — the hits_max>0 guard protects it."""
    cap = _run_capture(monkeypatch, _ctx(hits=0, hits_max=0))
    assert cap["interrupted"] is False
