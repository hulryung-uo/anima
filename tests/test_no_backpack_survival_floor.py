"""The no-backpack survival branch must heal across the same 30-40% HP band as
every other Priority-1 survival path.

When the backpack serial is momentarily undetected (a documented, recoverable
condition the planner handles with a 15s re-request loop), ``select_procedure``
runs a TRUNCATED survival check and returns early — the full Priority-1 ladder
(flee swarm, heal-in-place at 0.40, poison cure) below it is never reached.
That truncated check used a hardcoded ``ss.hits < ss.hits_max * 0.3`` floor,
recreating the exact 30-40% HP "dead zone" that commits b24d9cb / 3170a8f /
test_heal_in_place_band closed everywhere else: a backpack-less agent at 30-40%
HP got no heal-potion and bled down to 29% before reacting.

The branch now reuses ``_should_heal_in_place`` (the shared 0.40 floor), so the
no-backpack heal decision agrees with the rest of the survival ladder. Bandages
can't fire without a backpack, but ``heal_self`` (an on-hand potion quaff) can —
and now does — across the whole band.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner, _HEAL_IN_PLACE_HP_PCT
from anima.procedures.base import ProcedureRegistry


class _StubHeal:
    """Minimal heal_self stand-in: always startable, records that it ran."""

    name = "heal_self"

    async def can_start(self, ctx):
        return True


class _Registry(ProcedureRegistry):
    def __init__(self, heal):
        super().__init__()
        self._heal = heal

    def get(self, name):
        if name == "heal_self":
            return self._heal
        return None


def _ctx(hits, hits_max=100):
    # Backpack UNDETECTED: equipment has no 0x15 entry -> `backpack` is falsy,
    # driving select_procedure into the no-backpack branch.
    ss = SimpleNamespace(
        is_alive=True, x=100, y=100, serial=0x1,
        hits=hits, hits_max=hits_max, gold=0,
        weight=0, weight_max=400,
        is_poisoned=False, poison_level=-1,
        equipment={},  # .get(0x15) -> None
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


async def _noop_async(*a, **k):
    return None


def _planner(heal):
    return Planner(_Registry(heal))


def test_no_backpack_floor_matches_shared_survival_floor():
    # The fix's whole point: the no-backpack branch and the rest of the
    # survival ladder share one floor (0.40), not a stale hardcoded 0.30.
    assert _HEAL_IN_PLACE_HP_PCT == 0.40


@pytest.mark.parametrize("hits", [30, 34, 35, 39])
def test_heals_in_the_dead_zone_without_a_backpack(hits):
    # 30-40% HP, backpack undetected. Old hardcoded 0.30 floor left this band
    # untreated; the shared 0.40 floor now selects heal_self.
    heal = _StubHeal()
    planner = _planner(heal)
    proc = asyncio.run(planner.select_procedure(_ctx(hits)))
    assert proc is heal, f"expected heal_self at {hits}% HP, got {proc}"


@pytest.mark.parametrize("hits", [40, 55, 100])
def test_does_not_heal_at_or_above_the_floor_without_a_backpack(hits):
    # At/above 40% the agent is healthy enough; the no-backpack branch must not
    # pre-empt with a heal (it falls through to buy-tools / move / idle).
    heal = _StubHeal()
    planner = _planner(heal)
    proc = asyncio.run(planner.select_procedure(_ctx(hits)))
    assert proc is not heal, f"unexpected heal_self at {hits}% HP"


def test_below_30_still_heals_without_a_backpack():
    # The pre-existing behaviour (< 30% always heals) is preserved by the
    # widened floor.
    heal = _StubHeal()
    planner = _planner(heal)
    proc = asyncio.run(planner.select_procedure(_ctx(20)))
    assert proc is heal
