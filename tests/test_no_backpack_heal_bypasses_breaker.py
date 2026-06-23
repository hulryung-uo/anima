"""The no-backpack survival branch must bypass the anti-thrash starvation
breaker for its last-resort heal, exactly like the full Priority-1 ladder.

This branch runs in the post-resurrection NAKED state (the backpack serial is
not yet re-detected), where ``heal_self`` is the only heal that can fire —
bandages live in the undetected backpack. ``heal_self`` quaffs an on-hand
potion, and under sustained pressure its ``can_start`` fails a few times in a
row (no free hand, watchdog-cancelled tick), which demotes it in
``_proc_breaker`` after ``_STARVE_FAILS`` consecutive failures.

The Priority-1 heal-in-place block below this branch deliberately fetches
heal_self DIRECTLY so the breaker can never silence a survival heal. The
no-backpack branch used to honour the breaker (``not is_open("heal_self")``),
so a critically-wounded, backpack-less agent under sustained pressure would
get NO heal and bleed to death — the exact non-negotiable death the direct
fetch exists to prevent. This test pins the bypass.
"""
import asyncio
from types import SimpleNamespace

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


class _StubHeal:
    name = "heal_self"

    async def can_start(self, ctx):
        return True


class _Registry(ProcedureRegistry):
    def __init__(self, heal):
        super().__init__()
        self._heal = heal

    def get(self, name):
        return self._heal if name == "heal_self" else None


async def _noop_async(*a, **k):
    return None


def _ctx(hits, hits_max=100):
    # Backpack UNDETECTED (no 0x15) -> drives the no-backpack survival branch.
    ss = SimpleNamespace(
        is_alive=True, x=100, y=100, serial=0x1,
        hits=hits, hits_max=hits_max, gold=0,
        weight=0, weight_max=400,
        is_poisoned=False, poison_level=-1,
        equipment={},
    )
    world = SimpleNamespace(
        items={},
        nearby_mobiles=lambda x, y, distance=8: [],
    )
    perception = SimpleNamespace(self_state=ss, world=world)
    conn = SimpleNamespace(send_packet=_noop_async)
    return SimpleNamespace(
        perception=perception,
        blackboard={},
        conn=conn,
        bus=None,
        persona=SimpleNamespace(name="Tester", profession=""),
    )


def test_no_backpack_heal_fires_even_when_breaker_is_open():
    # A critically-wounded (20% HP), backpack-less agent whose heal_self has
    # been demoted by the starvation breaker must STILL heal — survival is
    # non-negotiable and must pierce the breaker.
    heal = _StubHeal()
    planner = Planner(_Registry(heal))
    # Trip the breaker: _STARVE_FAILS consecutive heal_self failures.
    for _ in range(planner._STARVE_FAILS):
        planner._proc_breaker.record_failure("heal_self")
    assert planner._proc_breaker.is_open("heal_self")

    proc = asyncio.run(planner.select_procedure(_ctx(20)))
    assert proc is heal, "survival heal must bypass the starvation breaker"
