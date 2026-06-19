"""Priority-1c survival gate: an active poison must preempt the profession /
mining loop, independent of HP.

The planner's two HP-percentage survival gates (_should_flee_swarm,
_should_heal_in_place) cannot see poison. A freshly-poisoned agent sits at high
HP, so neither fires and the agent falls through to the Priority-1.5 profession
loop — where combat_loop's in-fight poison-bandage override no longer runs (the
fight is over). The poison then ticks it to death. _should_cure_poison closes
that gap by selecting bandage_self the moment the agent is poisoned, and
bandage_self.can_start now admits a poisoned agent even at full HP.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.bandage_self import BandageSelf
from anima.procedures.base import ProcedureRegistry


def _ss(hits, hits_max=100, poisoned=False, poison_level=-1):
    return SimpleNamespace(
        is_alive=True, x=100, y=100, serial=0x1,
        hits=hits, hits_max=hits_max,
        is_poisoned=poisoned, poison_level=poison_level,
    )


def test_poison_gate_fires_at_high_hp():
    # The whole point: poisoned but healthy. The HP gates stay silent; the
    # poison gate must fire so the agent cure-bandages instead of grinding.
    planner = Planner(ProcedureRegistry())
    assert planner._should_heal_in_place(_ss(95, poisoned=True)) is False
    assert planner._should_cure_poison(_ss(95, poisoned=True, poison_level=2)) is True


def test_poison_gate_silent_when_not_poisoned():
    planner = Planner(ProcedureRegistry())
    assert planner._should_cure_poison(_ss(95, poisoned=False)) is False
    assert planner._should_cure_poison(_ss(50, poisoned=False)) is False


def _bandage_in_pack(ctx):
    # Stand in for find_in_backpack: one bandage item present.
    return [SimpleNamespace(serial=0xABC, graphic=0x0E21)]


def _can_start(proc, ctx, monkeypatch):
    import anima.procedures.bandage_self as bs
    monkeypatch.setattr(bs, "find_in_backpack", lambda c, g: _bandage_in_pack(c))
    return asyncio.run(proc.can_start(ctx))


def test_bandage_self_starts_when_poisoned_at_full_hp(monkeypatch):
    # Without the poison admission, can_start refuses at >= 95% HP and the
    # planner's poison gate would be a no-op (selects a proc that won't run).
    proc = BandageSelf()
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=_ss(100, poisoned=True, poison_level=1))
    )
    assert _can_start(proc, ctx, monkeypatch) is True


def test_bandage_self_still_refuses_full_hp_unpoisoned(monkeypatch):
    # Regression guard: a healthy, un-poisoned agent must NOT bandage (server
    # answers "not damaged", wasting a bandage and gaining nothing).
    proc = BandageSelf()
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=_ss(100, poisoned=False))
    )
    assert _can_start(proc, ctx, monkeypatch) is False


def test_bandage_self_starts_when_wounded(monkeypatch):
    proc = BandageSelf()
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=_ss(50, poisoned=False))
    )
    assert _can_start(proc, ctx, monkeypatch) is True
