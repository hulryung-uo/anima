"""Tests for the survival-floor flee gate (``_flee_destination`` / ``_FleeFromHostiles``).

The HP-floor guard only fires when the agent is wounded *and* swarmed — the
exact geometry where it tends to sit on the hostile centroid. The destination
computation must still break contact in that degenerate case instead of
"fleeing" to its own tile (a no-op that gets the agent killed mid-bandage).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
from anima.perception.enums import NotorietyFlag
from anima.planner.planner import (
    Planner,
    _FLEE_HP_PCT,
    _FleeFromHostiles,
    _count_hostiles,
    _flee_destination,
)
from anima.procedures.base import Procedure, ProcedureRegistry, ProcedureResult


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, is_dead=False, body=0x0021):
    # body 0x0021 = a non-human creature so _find_target (human-only-when-hostile
    # filter) treats it as a valid target. hits/hits_max present for focus-fire.
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, is_dead=is_dead,
        body=body, hits=50, hits_max=50,
    )


def _ss(x=100, y=100, serial=0x1):
    return SimpleNamespace(x=x, y=y, serial=serial, hits=10, hits_max=100)


def _chebyshev(ax, ay, bx, by) -> int:
    return max(abs(ax - bx), abs(ay - by))


class TestFleeDestination:
    def test_moves_away_from_offset_pack(self):
        ss = _ss()
        # Pack clustered to the SE → flee NW.
        hostiles = [_mob(0x2, 103, 103), _mob(0x3, 104, 102), _mob(0x4, 102, 104)]
        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        assert fx < ss.x and fy < ss.y

    def test_surrounded_centroid_does_not_flee_in_place(self):
        """Agent sits exactly on the centroid of a symmetric swarm.

        Old behaviour: away-vector is (0,0) → destination == agent tile → the
        agent never moves and dies. The destination must be a real tile some
        distance away.
        """
        ss = _ss(x=100, y=100)
        hostiles = [
            _mob(0x2, 99, 100), _mob(0x3, 101, 100),
            _mob(0x4, 100, 99), _mob(0x5, 100, 101),
        ]
        # Sanity: this swarm's centroid really is the agent's tile.
        cx = sum(m.x for m in hostiles) / len(hostiles)
        cy = sum(m.y for m in hostiles) / len(hostiles)
        assert (cx, cy) == (ss.x, ss.y)

        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        # Must actually break contact, not flee onto its own tile.
        assert (fx, fy) != (ss.x, ss.y)
        assert _chebyshev(ss.x, ss.y, fx, fy) >= 1

    def test_flee_distance_respected_on_offset_pack(self):
        ss = _ss(x=100, y=100)
        hostiles = [_mob(0x2, 110, 100)]  # due east → flee due west
        fx, fy = _flee_destination(ss, hostiles, flee_dist=12)
        assert fy == 100
        assert ss.x - fx == 12


def _ctx(self_x, self_y, mobiles):
    ss = _ss(x=self_x, y=self_y)

    def nearby(x, y, distance=12):
        return [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ]

    world = SimpleNamespace(nearby_mobiles=nearby)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
    )


class TestFleeFromHostilesRun:
    @pytest.mark.asyncio
    async def test_run_when_surrounded_moves_off_tile(self, monkeypatch):
        calls = {}

        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            calls["dest"] = (x, y)
            return True

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(100, 100, [
            _mob(0x2, 99, 100), _mob(0x3, 101, 100),
            _mob(0x4, 100, 99), _mob(0x5, 100, 101),
        ])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        # The agent must NOT have "fled" to its own tile.
        assert calls["dest"] != (100, 100)
        assert _chebyshev(100, 100, *calls["dest"]) >= 1
        assert result.next_suggestion == "bandage_self"

    @pytest.mark.asyncio
    async def test_run_no_hostiles_is_noop(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            raise AssertionError("should not move when nothing to flee from")

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(100, 100, [])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        assert "No hostiles" in result.message


class TestHostileCountExcludesCorpses:
    """Right after a kill, the felled mob lingers in world.mobiles with its
    enemy notoriety intact until 0x1D Delete. The survival flee gate must not
    count those corpses, or a wounded agent flees from dead bodies instead of
    healing in place on a fight it already won.
    """

    def test_count_hostiles_skips_dead_mobs(self):
        ctx = _ctx(100, 100, [
            _mob(0x2, 101, 100, is_dead=True),
            _mob(0x3, 100, 101, is_dead=True),
            _mob(0x4, 99, 100, is_dead=True),
        ])
        ss = ctx.perception.self_state
        # All three are corpses → no live hostiles, so the flee gate stays off.
        assert _count_hostiles(ctx, ss, dist=6) == 0

    def test_count_hostiles_counts_only_living(self):
        ctx = _ctx(100, 100, [
            _mob(0x2, 101, 100, is_dead=True),   # corpse
            _mob(0x3, 100, 101, is_dead=False),  # alive
            _mob(0x4, 99, 100),                  # alive (default)
        ])
        ss = ctx.perception.self_state
        assert _count_hostiles(ctx, ss, dist=6) == 2

    def test_count_hostiles_ignores_missing_is_dead_attr(self):
        # A mob object that omits is_dead entirely (e.g. a bare stub) must be
        # treated as alive — never silently dropped as a corpse.
        live = SimpleNamespace(serial=0x5, x=101, y=100, notoriety=NotorietyFlag.ENEMY)
        ctx = _ctx(100, 100, [live])
        ss = ctx.perception.self_state
        assert _count_hostiles(ctx, ss, dist=6) == 1

    @pytest.mark.asyncio
    async def test_flee_run_treats_all_corpses_as_no_hostiles(self, monkeypatch):
        async def fake_go_to(ctx, x, y, run=False, interrupt_check=None):
            raise AssertionError("should not flee from corpses")

        monkeypatch.setattr(movement, "go_to", fake_go_to)
        ctx = _ctx(100, 100, [
            _mob(0x2, 99, 100, is_dead=True),
            _mob(0x3, 101, 100, is_dead=True),
        ])
        result = await _FleeFromHostiles().run(ctx)
        assert result.success is True
        assert "No hostiles" in result.message


class _StubProc(Procedure):
    def __init__(self, name: str):
        self.name = name
        self.description = f"stub {name}"

    async def can_start(self, ctx):
        return True

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} ran")


def _planner_ctx(hits: int, mobiles):
    """Minimal AgentContext for Planner.select_procedure with a swarm nearby."""
    ss = SimpleNamespace(
        x=100, y=100, z=0, serial=0x1,
        hits=hits, hits_max=100,
        hp_percent=hits / 100.0 * 100.0,  # _bandage_should_yield_to_combat reads this
        weight=100, weight_max=400, gold=50,
        is_alive=True,
        equipment={0x15: 0x101},
        skills={},
    )

    def nearby(x, y, distance=12):
        return [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ]

    world = SimpleNamespace(nearby_mobiles=nearby, items={})
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        bus=None,
        persona=SimpleNamespace(profession="adventurer", name="t"),
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
        memory_db=None,
        # No map loaded in this unit context — the mining/gathering scans that
        # select_procedure reaches when the agent does NOT flee short-circuit
        # cleanly on a None map_reader instead of touching real .mul data.
        map_reader=None,
    )
    return ctx


class TestSwarmFleeDeathZone:
    """Regression: a wounded, surrounded warrior must FLEE across the whole
    30–40% HP band, not just below 30%. At 35% HP a hunt has already broken
    off (RETREAT_HP_PCT=35); if the planner then bandages in place, the swarm
    cancels the bandage and the agent dies. The flee gate must close that zone.
    """

    def _swarm(self):
        return [_mob(0x2, 99, 100), _mob(0x3, 101, 100), _mob(0x4, 100, 99)]

    @pytest.mark.asyncio
    async def test_flees_in_30_to_40_band(self):
        reg = ProcedureRegistry()
        reg.register(_StubProc("heal_self"))
        reg.register(_StubProc("bandage_self"))
        planner = Planner(reg)
        # 35% HP: above the old 0.30 floor (no flee) but inside the swarm
        # death zone the raised floor now covers.
        ctx = _planner_ctx(hits=35, mobiles=self._swarm())
        proc = await planner.select_procedure(ctx)
        assert proc.name == "flee_from_hostiles"

    @pytest.mark.asyncio
    async def test_no_flee_above_band(self):
        reg = ProcedureRegistry()
        reg.register(_StubProc("heal_self"))
        planner = Planner(reg)
        # 45% HP — above the flee floor; must NOT flee even when swarmed.
        ctx = _planner_ctx(hits=45, mobiles=self._swarm())
        proc = await planner.select_procedure(ctx)
        assert proc is None or proc.name != "flee_from_hostiles"

    @pytest.mark.asyncio
    async def test_flee_floor_exceeds_combat_retreat(self):
        # The flee floor must sit at or above combat's 35% retreat floor, or
        # the death zone reopens.
        from anima.procedures.combat_loop import RETREAT_HP_PCT
        assert _FLEE_HP_PCT * 100 >= RETREAT_HP_PCT
