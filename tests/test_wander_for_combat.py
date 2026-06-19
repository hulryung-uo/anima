"""Tests for WanderForCombat — the combat re-engage step that stops a weaponed
warrior from idling into the mining loop when no target is in range.

can_start must be the exact complement of HuntNearby on the target condition:
armed + a target in range → hunt handles it (wander stays off); armed + NO
target → wander roams to find one (instead of mining fallthrough).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import WanderForCombat
from anima.perception.enums import NotorietyFlag


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, body=0x0021):
    return SimpleNamespace(serial=serial, x=x, y=y, notoriety=notoriety, body=body)


def _ctx(self_x=100, self_y=100, mobiles=(), armed=True, hp_pct=100.0):
    ss = SimpleNamespace(
        is_alive=True, hits_max=100, hp_percent=hp_pct,
        x=self_x, y=self_y, serial=0x1,
        equipment={1: 0x999} if armed else {},
    )
    world = SimpleNamespace(nearby_mobiles=lambda x, y, distance=0: list(mobiles))
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
    )


class TestCanStart:
    @pytest.mark.asyncio
    async def test_armed_no_target_activates(self):
        ctx = _ctx(mobiles=[])
        assert await WanderForCombat().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_armed_with_target_in_range_stays_off(self):
        # hunt_nearby owns this case; wander must NOT fire.
        ctx = _ctx(mobiles=[_mob(0x2, 102, 101)])  # within ENGAGE_RANGE=10
        assert await WanderForCombat().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_unarmed_stays_off(self, monkeypatch):
        monkeypatch.setattr(cl, "find_in_backpack", lambda ctx, g: [])
        ctx = _ctx(mobiles=[], armed=False)
        assert await WanderForCombat().can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_low_hp_stays_off(self):
        ctx = _ctx(mobiles=[], hp_pct=20.0)
        assert await WanderForCombat().can_start(ctx) is False


class TestExecute:
    @pytest.mark.asyncio
    async def test_hands_back_to_hunt_when_target_spotted(self, monkeypatch):
        # go_to "finds" a target mid-walk: invoke interrupt_check after a mob
        # appears in the world.
        appeared = [_mob(0x2, 105, 100)]

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            # mob is now in range; the interrupt should detect it
            if interrupt_check:
                interrupt_check()

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=appeared)
        # can_start would be False now (target present), but execute is called
        # right after the walk; simulate: no target at start, target after walk.
        ctx.perception.world.nearby_mobiles = lambda x, y, distance=0: list(appeared)
        result = await WanderForCombat().execute(ctx)
        assert result.success is True
        assert result.next_suggestion == "hunt_nearby"

    @pytest.mark.asyncio
    async def test_keeps_wandering_when_still_empty(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=[])
        result = await WanderForCombat().execute(ctx)
        assert result.success is True                       # liveness, no fallthrough
        assert result.next_suggestion == "wander_for_combat"

    @pytest.mark.asyncio
    async def test_rotates_direction_each_run(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None
        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=[])
        proc = WanderForCombat()
        await proc.execute(ctx)
        assert ctx.blackboard["_wander_dir_idx"] == 1
        await proc.execute(ctx)
        assert ctx.blackboard["_wander_dir_idx"] == 2

    @pytest.mark.asyncio
    async def test_orbit_center_stays_fixed_while_agent_drifts(self, monkeypatch):
        """Regression: with no prior fight, combat_anchor is unset. The orbit
        center must be PINNED on the first sweep, not re-derived from the
        agent's drifting position each tick — otherwise the warrior walks
        unboundedly away from the spawn arena instead of orbiting it."""
        async def drifting_go_to(ctx, x, y, run=False, interrupt_check=None):
            ss = ctx.perception.self_state
            ss.x += (1 if x > ss.x else -1 if x < ss.x else 0)
            ss.y += (1 if y > ss.y else -1 if y < ss.y else 0)
            return None

        destinations: list[tuple[int, int]] = []

        async def recording_go_to(ctx, x, y, run=False, interrupt_check=None):
            destinations.append((x, y))
            return await drifting_go_to(ctx, x, y, run=run, interrupt_check=interrupt_check)

        monkeypatch.setattr(movement, "go_to", recording_go_to)

        ctx = _ctx(self_x=100, self_y=100, mobiles=[])
        assert "combat_anchor" not in ctx.blackboard  # no fight ran first
        proc = WanderForCombat()
        for _ in range(3):
            await proc.execute(ctx)

        assert ctx.blackboard["combat_anchor"] == (100, 100)
        assert (ctx.perception.self_state.x, ctx.perception.self_state.y) != (100, 100)
        from anima.procedures.combat_loop import WANDER_RADIUS
        for dx, dy in destinations:
            assert abs(dx - 100) <= WANDER_RADIUS
            assert abs(dy - 100) <= WANDER_RADIUS

    @pytest.mark.asyncio
    async def test_respects_existing_anchor_from_a_prior_fight(self, monkeypatch):
        """When HuntNearby already set combat_anchor, wander must orbit THAT
        anchor, not the agent's current position (setdefault is a no-op)."""
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(self_x=200, self_y=200, mobiles=[])
        ctx.blackboard["combat_anchor"] = (50, 50)  # from a previous fight
        await WanderForCombat().execute(ctx)
        assert ctx.blackboard["combat_anchor"] == (50, 50)

    @pytest.mark.asyncio
    async def test_yields_to_other_work_after_max_empty_roams(self, monkeypatch):
        # Depleted arena (no hostiles ever): wander a few rounds then YIELD
        # (success=False) so the planner falls through to productive work,
        # instead of roaming until the 60s health-break fires.
        from anima.procedures.combat_loop import WANDER_MAX_EMPTY

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=[])
        proc = WanderForCombat()
        results = [await proc.execute(ctx) for _ in range(WANDER_MAX_EMPTY)]
        assert all(r.success for r in results[:-1])      # earlier roams keep trying
        assert results[-1].success is False              # final sweep yields
        assert ctx.blackboard["_wander_empty"] == 0       # reset → can retry later
        # ...but a yield arms a cooldown so the agent doesn't immediately re-arm
        # wander and flap away from the productive work it just yielded to.
        assert ctx.blackboard["_wander_cooldown_until"] > 0.0


class TestYieldCooldown:
    """Mode-transition contract: after wander yields to productive work, it must
    stay OFF for a cooldown window so that work actually runs — otherwise
    can_start (armed + no target) re-fires the very next tick and the adventurer
    flaps wander↔mine, doing at most one productive invocation between sweeps."""

    @pytest.mark.asyncio
    async def test_can_start_false_during_cooldown(self, monkeypatch):
        import anima.procedures.combat_loop as clmod

        ctx = _ctx(mobiles=[])  # armed, no target → would normally activate
        assert await WanderForCombat().can_start(ctx) is True
        # Simulate a just-fired yield arming the cooldown.
        now = 1_000.0
        monkeypatch.setattr(clmod.time, "time", lambda: now)
        ctx.blackboard["_wander_cooldown_until"] = now + clmod.WANDER_YIELD_COOLDOWN_S
        # During the window wander must stay off so productive work can run.
        assert await WanderForCombat().can_start(ctx) is False
        # After the window lapses it re-arms (open world respawns hostiles).
        monkeypatch.setattr(clmod.time, "time", lambda: now + clmod.WANDER_YIELD_COOLDOWN_S + 1)
        assert await WanderForCombat().can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_yield_then_no_immediate_reentry(self, monkeypatch):
        # End-to-end: drive execute to the yield, then assert can_start is
        # suppressed (the flap the fix prevents).
        import anima.procedures.combat_loop as clmod
        from anima.procedures.combat_loop import WANDER_MAX_EMPTY

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            return None

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        now = 5_000.0
        monkeypatch.setattr(clmod.time, "time", lambda: now)
        ctx = _ctx(mobiles=[])
        proc = WanderForCombat()
        for _ in range(WANDER_MAX_EMPTY):
            await proc.execute(ctx)
        # The final sweep armed the cooldown; wander must now refuse to start.
        assert await proc.can_start(ctx) is False

    @pytest.mark.asyncio
    async def test_spotted_target_clears_cooldown(self, monkeypatch):
        # If a fight reappears, a stale yield cooldown must not block hunting.
        appeared = [_mob(0x2, 105, 100)]

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            if interrupt_check:
                interrupt_check()

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(mobiles=appeared)
        ctx.perception.world.nearby_mobiles = lambda x, y, distance=0: list(appeared)
        ctx.blackboard["_wander_cooldown_until"] = 9_999_999_999.0  # stale
        result = await WanderForCombat().execute(ctx)
        assert result.success is True
        assert result.next_suggestion == "hunt_nearby"
        assert ctx.blackboard["_wander_cooldown_until"] == 0.0
