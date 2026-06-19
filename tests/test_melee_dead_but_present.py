"""MeleeAttack credits a kill the tick the target's health bar reads zero,
even while the body still lingers in world.mobiles.

Regression: UO leaves a just-killed mob in world.mobiles (corpse/ghost body)
until its 0x1D Delete arrives, which can lag several seconds. The engage loop
only confirmed a kill in the `mob is None` branch, so a dead-but-present mob
(hits_max>0, hits<=0) was re-attacked every tick until COMBAT_TIMEOUT (30s)
expired — then MeleeAttack returned success=False with a -5 reward for a foe it
had actually felled, inverting the Q-learning signal. The procedures path
(combat_loop) already credits `current.hits_max > 0 and current.hits <= 0`;
this mirrors it.

asyncio.sleep and time.monotonic are mocked so the test never touches the wall
clock or the event loop, per the suite convention.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.skills.combat.melee import MeleeAttack


class _Clock:
    """Monotonic clock that advances one COMBAT_TICK per call so the engage
    loop runs a bounded number of iterations regardless of the fix."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def _make_ctx(mob: MobileInfo):
    ss = SimpleNamespace(
        x=100, y=100, z=0, serial=0x1,
        hits=80, hits_max=100, hp_percent=80.0,
        last_damage_taken_at=0.0,
    )
    world = SimpleNamespace(
        mobiles={mob.serial: mob},
        nearby_mobiles=lambda x, y, distance=0: [mob],
    )
    conn = SimpleNamespace(send_packet=AsyncMock())
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        conn=conn,
        blackboard={"persona": SimpleNamespace(combat_disposition="aggressive")},
    )


@pytest.mark.asyncio
async def test_dead_but_present_mob_is_credited_as_a_kill():
    # Target is in melee range and already at zero HP, but its body is still
    # in world.mobiles (Delete hasn't arrived). No corpse on the ground, so the
    # ONLY proof of death is the known-zero health bar — the fix must use it.
    mob = MobileInfo(
        serial=0x11, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=0,
    )
    ctx = _make_ctx(mob)

    with patch("anima.skills.combat.melee.time.monotonic", _Clock()), \
         patch("anima.skills.combat.melee.asyncio.sleep", new=AsyncMock()), \
         patch("anima.skills.combat.melee.find_corpses", return_value=[]):
        result = await MeleeAttack().execute(ctx)

    assert result.success is True, "a known zero-HP target is a confirmed kill"
    assert result.reward > 0, "a kill must carry a positive reward, not -5"
    assert "Killed" in result.message


@pytest.mark.asyncio
async def test_live_mob_that_kites_out_of_view_is_not_credited():
    # Control: a healthy target that simply disappears (Delete with no corpse,
    # no prior zero reading) must still NOT be credited — guard against the fix
    # over-crediting. It vanishes after the first tick.
    mob = MobileInfo(
        serial=0x12, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=150,
    )
    ctx = _make_ctx(mob)

    real_sleep = AsyncMock()

    async def _vanish(_delay):
        # After the war-mode/attack sleeps, drop the mob mid-loop so the
        # `mob is None` branch runs with no death evidence.
        ctx.perception.world.mobiles.pop(0x12, None)
        await real_sleep(_delay)

    with patch("anima.skills.combat.melee.time.monotonic", _Clock()), \
         patch("anima.skills.combat.melee.asyncio.sleep", new=_vanish), \
         patch("anima.skills.combat.melee.find_corpses", return_value=[]):
        result = await MeleeAttack().execute(ctx)

    assert result.success is False, "a kiter that fled with no corpse is not a kill"
    assert result.reward < 0
