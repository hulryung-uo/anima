"""Survival priority-ladder ordering: a freshly-resurrected agent that revives
into a swarm (the arena spawn is co-located with the resurrection spot) at low
HP must FLEE before walking back to its corpse to recover gear.

Bug: Priority 1a (_RecoverAfterDeath) ran unconditionally ahead of the
Priority 1b flee gate, so the agent marched the (weaponless, ~10% HP) body
straight back through the swarm to its corpse and was beaten down before it
could loot/re-equip — the die->res->naked->die loop, re-expressed as recovery
preempting flight.
"""
from types import SimpleNamespace

import pytest

import anima.actions.loot as loot
import anima.planner.planner as P
from anima.perception.enums import NotorietyFlag
from anima.procedures.base import ProcedureRegistry
from anima.planner.planner import Planner


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety,
        is_dead=False, is_yellow_health=False, body=0x00C8,
    )


def _revived_ctx(*, hits, mobiles=(), poisoned=False):
    """Freshly-revived agent: _was_dead set, alive, weaponless, near its corpse."""
    ss = SimpleNamespace(
        is_alive=True, hits=hits, hits_max=100, x=100, y=100, z=0,
        serial=0x1, gold=0, weight=0, weight_max=400,
        equipment={0x15: 0x500},  # backpack, but NO weapon (weaponless)
        is_poisoned=poisoned, poison_level=1 if poisoned else 0,
    )
    world = SimpleNamespace(
        items={},
        nearby_mobiles=lambda x, y, distance=0: [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ],
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={"_was_dead": True},
        bus=None,
    )


@pytest.fixture(autouse=True)
def _corpse_present(monkeypatch):
    # A recoverable corpse is always present so _RecoverAfterDeath.can_start
    # would otherwise fire; the test proves the FLEE gate wins instead.
    corpse = SimpleNamespace(serial=0x900, x=100, y=100)
    monkeypatch.setattr(loot, "find_corpses", lambda ctx, max_dist=0: [corpse])


class TestPostResRecoveryYieldsToFlee:
    @pytest.mark.asyncio
    async def test_revived_into_swarm_at_low_hp_flees_first(self):
        planner = Planner(ProcedureRegistry())
        # Revived at ~10% HP into a 3-mob swarm hugging the spawn point.
        ctx = _revived_ctx(hits=10, mobiles=[
            _mob(0x2, 101, 100), _mob(0x3, 100, 101), _mob(0x4, 102, 101),
        ])
        proc = await planner.select_procedure(ctx)
        # FLEE must win over corpse recovery.
        assert getattr(proc, "name", None) == "flee_from_hostiles"
        # And the recovery intent must NOT have been latched, while _was_dead
        # is preserved so recovery resumes after contact is broken.
        assert ctx.blackboard.get("_was_dead") is True
        # Counter clear (the existing dead->alive invariant) still happened.
        assert planner._repeat_counter.get("seek_resurrection", 0) == 0

    @pytest.mark.asyncio
    async def test_revived_into_swarm_when_poisoned_flees_first(self):
        planner = Planner(ProcedureRegistry())
        # Healthy HP but poisoned + swarmed → still flee before recovering.
        ctx = _revived_ctx(hits=95, poisoned=True, mobiles=[
            _mob(0x2, 101, 100), _mob(0x3, 100, 101), _mob(0x4, 102, 101),
        ])
        proc = await planner.select_procedure(ctx)
        assert getattr(proc, "name", None) == "flee_from_hostiles"
        assert ctx.blackboard.get("_was_dead") is True

    @pytest.mark.asyncio
    async def test_revived_safe_recovers_corpse(self):
        planner = Planner(ProcedureRegistry())
        # Low HP but NO swarm (revived clear of the mobs) → recover the corpse;
        # the flee yield must not steal a safe recovery.
        ctx = _revived_ctx(hits=10, mobiles=[])
        proc = await planner.select_procedure(ctx)
        assert getattr(proc, "name", None) == "recover_after_death"

    @pytest.mark.asyncio
    async def test_revived_low_hp_single_hostile_recovers(self):
        planner = Planner(ProcedureRegistry())
        # Wounded but only ONE hostile (below the swarm floor) → not a swarm,
        # so recovery proceeds rather than yielding to flee.
        ctx = _revived_ctx(hits=10, mobiles=[_mob(0x2, 101, 100)])
        proc = await planner.select_procedure(ctx)
        assert getattr(proc, "name", None) == "recover_after_death"

    def test_yield_predicate_does_not_touch_flee_counter(self):
        planner = Planner(ProcedureRegistry())
        ss = SimpleNamespace(hits=10, hits_max=100, x=100, y=100, serial=0x1,
                             is_poisoned=False)
        ctx = _revived_ctx(hits=10, mobiles=[
            _mob(0x2, 101, 100), _mob(0x3, 100, 101), _mob(0x4, 102, 101),
        ])
        # The non-side-effecting predicate must NOT advance the flee
        # consecutive counter (that is single-sourced from Priority 1b).
        before = planner._flee_consecutive
        assert planner._post_res_should_yield_to_flee(ctx, ctx.perception.self_state) is True
        assert planner._flee_consecutive == before
