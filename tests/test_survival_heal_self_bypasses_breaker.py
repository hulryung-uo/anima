"""Regression: a critically-wounded agent's potion heal (``heal_self``) must
not be silenced by the anti-thrash starvation breaker.

Symmetric to ``test_survival_bandage_bypasses_breaker``. The Priority-1
heal-in-place block tries ``heal_self`` (quaff an on-hand potion) first, then
falls through to ``bandage_self``. The bug: ``heal_self`` was gated on
``not self._proc_breaker.is_open("heal_self")``, so once a few interrupted /
watchdog-cancelled attempts demoted it, the gate returned ``None`` — and with
NO usable bandages (a mage, or bandages consumed), the bandage fallback also
yields ``None``, so BOTH survival heals go silent and the wounded agent falls
through to the profession / mining loop and bleeds to death. Survival heals
are non-negotiable: the starvation breaker is an anti-thrash convenience for
lower-priority work, never a reason to stop healing when critically wounded.
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
    """A wounded (< 40% HP), NOT-swarmed, NOT-dead agent with a backpack.

    A non-bandage item (graphic 0x0EED gold) keeps bp_items > 0 so the
    planner's backpack-content-refresh branch (which needs ctx.conn) is
    skipped. There is deliberately NO bandage in the pack, so the bandage
    fallback cannot rescue a breaker-silenced ``heal_self``.
    """
    coin = SimpleNamespace(container=0x101, graphic=0x0EED, amount=5, hue=0, serial=0x9)
    world = SimpleNamespace(
        items={0x9: coin}, nearby_mobiles=lambda x, y, distance=0: [],
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
async def test_heal_self_survives_a_demoted_starvation_breaker():
    """heal_self demoted by the breaker + no bandages available → the planner
    must STILL select heal_self, not fall through to the grind."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=True))       # a potion IS available
    reg.register(_Stub("bandage_self", can=False))   # no usable bandages
    reg.register(_Stub("mine_ore", can=True))        # the grind it would wrongly fall to
    planner = Planner(reg)

    # Demote heal_self via the starvation breaker (3 consecutive failures).
    for _ in range(planner._STARVE_FAILS):
        planner._proc_breaker.record_failure("heal_self")
    assert planner._proc_breaker.is_open("heal_self")

    proc = await planner.select_procedure(_ctx())
    assert proc is not None
    assert proc.name == "heal_self", (
        "critically-wounded potion heal must bypass the starvation breaker, "
        f"got {proc.name!r}"
    )


@pytest.mark.asyncio
async def test_heal_self_still_selected_when_breaker_closed():
    """Control: with the breaker closed, the same path selects heal_self."""
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=True))
    reg.register(_Stub("bandage_self", can=False))
    reg.register(_Stub("mine_ore", can=True))
    planner = Planner(reg)

    proc = await planner.select_procedure(_ctx())
    assert proc is not None and proc.name == "heal_self"
