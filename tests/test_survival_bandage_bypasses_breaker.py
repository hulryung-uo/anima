"""Regression: a critically-wounded agent's last-resort bandage heal must not
be silenced by the anti-thrash starvation breaker.

The Priority-1 heal-in-place block fetches ``heal_self`` directly from the
registry (only gating it on its own breaker check) and, when that is
unavailable, falls through to ``bandage_self``. The bug: that bandage fallback
used to go through ``_get_proc``, which returns ``None`` whenever the
starvation breaker (``self._proc_breaker``) has demoted the procedure after a
few consecutive failures. A bandage interrupted by an adjacent mob fails, so
under sustained pressure the breaker demotes ``bandage_self`` — and then, with
``heal_self`` also unavailable (no potions), BOTH survival heals return ``None``
and the wounded agent falls straight through to the profession / mining loop and
bleeds to death. Survival heals are non-negotiable: the breaker is an
anti-thrash convenience for lower-priority work, never a reason to stop healing
when critically wounded.
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
    """A wounded (< 40% HP), NOT-swarmed, NOT-dead agent with a backpack."""
    # A bandage in the pack (graphic 0x0E21) keeps bp_items > 0 so the
    # planner's backpack-content-refresh branch (which needs ctx.conn) is
    # skipped. amount/hue/serial present so all inventory scans tolerate it.
    bandage = SimpleNamespace(container=0x101, graphic=0x0E21, amount=5, hue=0, serial=0x9)
    world = SimpleNamespace(
        items={0x9: bandage}, nearby_mobiles=lambda x, y, distance=0: [],
    )
    self_state = SimpleNamespace(
        is_alive=True, x=100, y=100, z=0, serial=0x1,
        hits=20, hits_max=100, weight=100, weight_max=400, gold=50,
        equipment={0x15: 0x101}, is_poisoned=False,
    )
    perception = SimpleNamespace(self_state=self_state, world=world)
    return SimpleNamespace(
        perception=perception, blackboard={}, bus=None,
        persona=SimpleNamespace(name="T", profession=""),
    )


@pytest.mark.asyncio
async def test_bandage_survives_a_demoted_starvation_breaker():
    """heal_self unavailable + bandage_self demoted by the breaker → the
    planner must STILL select bandage_self, not fall through to the grind."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))  # no potions
    reg.register(_Stub("bandage_self", can=True))
    reg.register(_Stub("mine_ore", can=True))  # the grind it would wrongly fall to
    planner = Planner(reg)

    # Demote bandage_self via the starvation breaker (3 consecutive failures).
    for _ in range(planner._STARVE_FAILS):
        planner._proc_breaker.record_failure("bandage_self")
    assert planner._proc_breaker.is_open("bandage_self")

    proc = await planner.select_procedure(_ctx())
    assert proc is not None
    assert proc.name == "bandage_self", (
        "critically-wounded bandage heal must bypass the starvation breaker, "
        f"got {proc.name!r}"
    )


@pytest.mark.asyncio
async def test_bandage_still_selected_when_breaker_closed():
    """Control: with the breaker closed, the same path selects bandage_self."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))
    reg.register(_Stub("bandage_self", can=True))
    reg.register(_Stub("mine_ore", can=True))
    planner = Planner(reg)

    proc = await planner.select_procedure(_ctx())
    assert proc is not None and proc.name == "bandage_self"
