"""Regression: the Priority-1c poison cure-bandage must not be silenced by the
anti-thrash starvation breaker.

A freshly-poisoned agent sits at high HP, so neither HP-percentage survival
gate (_should_flee_swarm, _should_heal_in_place) fires. The poison gate
(_should_cure_poison) is the only thing standing between the agent and a slow
poison death while it grinds. Poison almost always lands in combat, where an
adjacent mob repeatedly interrupts the bandage — the exact consecutive-failure
pattern that demotes ``bandage_self`` in the starvation breaker
(``self._proc_breaker``). The bug: the poison gate used to fetch the cure via
``_get_proc``, which returns ``None`` for any breaker-demoted procedure, so the
poisoned agent fell straight through to the profession / mining loop and bled
to death. Cures are a survival action, non-negotiable; the breaker is an
anti-thrash convenience for lower-priority work only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import Procedure, ProcedureResult, ProcedureRegistry


class _Stub(Procedure):
    def __init__(self, name: str, can: bool = True):
        self.name = name
        self.description = f"stub {name}"
        self._can = can

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} done")


def _ctx():
    """A POISONED, full-HP, NOT-swarmed, NOT-dead agent with a backpack.

    Full HP keeps the _should_heal_in_place / _should_flee_swarm gates silent
    so the poison gate is the only survival branch in play. A bandage in the
    pack keeps bp_items > 0 so the backpack-refresh branch (needs ctx.conn) is
    skipped.
    """
    bandage = SimpleNamespace(container=0x101, graphic=0x0E21, amount=5, hue=0, serial=0x9)
    world = SimpleNamespace(
        items={0x9: bandage}, nearby_mobiles=lambda x, y, distance=0: [],
    )
    self_state = SimpleNamespace(
        is_alive=True, x=100, y=100, z=0, serial=0x1,
        hits=100, hits_max=100, weight=100, weight_max=400, gold=50,
        equipment={0x15: 0x101}, is_poisoned=True, poison_level=2,
    )
    perception = SimpleNamespace(self_state=self_state, world=world)
    return SimpleNamespace(
        perception=perception, blackboard={}, bus=None,
        persona=SimpleNamespace(name="T", profession=""),
    )


@pytest.mark.asyncio
async def test_poison_cure_survives_a_demoted_starvation_breaker():
    """bandage_self demoted by the breaker → the poison gate must STILL select
    it, not fall through to the grind and bleed to death."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))  # no potions
    reg.register(_Stub("bandage_self", can=True))
    reg.register(_Stub("mine_ore", can=True))  # the grind it would wrongly fall to
    planner = Planner(reg)

    # Demote bandage_self via the starvation breaker (consecutive failures).
    for _ in range(planner._STARVE_FAILS):
        planner._proc_breaker.record_failure("bandage_self")
    assert planner._proc_breaker.is_open("bandage_self")

    proc = await planner.select_procedure(_ctx())
    assert proc is not None
    assert proc.name == "bandage_self", (
        "poison cure-bandage must bypass the starvation breaker, "
        f"got {proc.name!r}"
    )


@pytest.mark.asyncio
async def test_poison_cure_selected_when_breaker_closed():
    """Control: with the breaker closed, the same path selects bandage_self."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))
    reg.register(_Stub("bandage_self", can=True))
    reg.register(_Stub("mine_ore", can=True))
    planner = Planner(reg)

    proc = await planner.select_procedure(_ctx())
    assert proc is not None and proc.name == "bandage_self"
