"""A ghost-body death must break the hunt loop even when ``hits_max`` reads 0.

On death ServUO flips the player to a ghost body — the authoritative death
signal (``SelfState.is_ghost`` / ClassicUO ``Mobile.IsDead``). The health-bar
packets are NOT a reliable death oracle for self: a 0x78 ghost-incoming can
recreate self with ``hits_max == 0``, or an out-of-order 0xA1 can land carrying
stale pre-death ``hits``. The old death short-circuit gated solely on
``hits_max > 0 and not is_alive`` — so a ghost whose ``hits_max`` had been reset
to 0 silenced the HP arm and the loop kept spinning a corpse for the full 45s
``ENGAGEMENT_CAP_S`` (firing attack/bandage/potion packets the server rejects)
while the planner's Priority-0 resurrection branch sat blocked behind it. The
fix adds the body-based oracle: a ghost breaks the loop regardless of the bar.

``asyncio.sleep`` and ``time.monotonic`` are both mocked so the test is
deterministic and never actually waits.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import _GHOST_BODIES
from anima.procedures.combat_loop import HuntNearby

LIVING_BODY = 0x0190
GHOST_BODY = next(iter(_GHOST_BODIES))  # a real ServUO ghost graphic


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=hits, hits_max=hits_max,
    )


class _SS:
    """Self state whose death oracle is the ghost BODY, not the HP bar.

    Mirrors the real ``SelfState``: ``is_ghost`` keys off the body graphic and
    ``is_alive`` is False for a ghost regardless of hits/hits_max (a ghost can
    show a non-zero or unknown bar). Modelling the body-flip death — with
    ``hits_max`` reset to 0 — is exactly the case the HP-only guard missed.
    """

    def __init__(self):
        self.x = 100
        self.y = 100
        self.serial = 0x1
        self.body = LIVING_BODY
        self.hits = 100
        self.hits_max = 100
        self.stam = 100
        self.stam_max = 100
        self.equipment = {1: 0xDEAD}
        self.skills: dict = {}

    @property
    def hp_percent(self):
        return (self.hits / self.hits_max) * 100.0 if self.hits_max else 100.0

    @property
    def is_ghost(self):
        return self.body in _GHOST_BODIES

    @property
    def is_alive(self):
        if self.is_ghost:
            return False
        return self.hits > 0 or self.hits_max == 0


def _ctx(mobiles):
    ss = _SS()

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items={})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def _install_clocks(monkeypatch, on_tick):
    """Mock asyncio.sleep (calls on_tick) and a monotonic clock that never ends.

    monotonic advances a hair each call so the ENGAGEMENT_CAP_S deadline is NOT
    reached on its own — the loop can only exit via the death short-circuit.
    """
    clock = {"t": 1000.0}

    def fake_monotonic():
        clock["t"] += 0.001
        return clock["t"]

    async def fake_sleep(_):
        on_tick()
        return None

    monkeypatch.setattr(cl.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(cl.asyncio, "sleep", fake_sleep)


@pytest.mark.asyncio
async def test_ghost_body_with_zero_hits_max_breaks_loop(monkeypatch):
    # One adjacent live hostile (no chase needed). On the first tick the agent
    # dies: the body flips to a ghost graphic AND hits_max is reset to 0 (the
    # 0x78 ghost-incoming case). The HP arm (hits_max > 0) is silenced, so only
    # the body oracle can break the loop.
    foe = _mob(0x2, 101, 100)
    ctx = _ctx({foe.serial: foe})

    go_to_calls: list[tuple] = []

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_calls.append((x, y))
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    # No bandages / potions in the pack (and no weapon equip needed: layer 1 set).
    monkeypatch.setattr(cl, "find_in_backpack", lambda *a, **k: [])

    ticks = {"n": 0}

    def _die_on_first_tick():
        ticks["n"] += 1
        if ticks["n"] == 1:
            ss = ctx.perception.self_state
            ss.body = GHOST_BODY   # authoritative death signal
            ss.hits_max = 0        # 0x78 ghost-incoming silences the HP arm
            ss.hits = 0

    _install_clocks(monkeypatch, _die_on_first_tick)

    result = await HuntNearby().execute(ctx)

    # Without the body oracle the loop would spin the full cap; it must bail on
    # the very tick the ghost body appears.
    assert ticks["n"] == 1, "a ghost-body death must break the loop at once"
    assert result.success is False
    assert result.next_suggestion is None  # yield to the resurrection branch
    assert "died" in result.message
    assert go_to_calls == [], "no anchor walk-back while dead (ghost)"
