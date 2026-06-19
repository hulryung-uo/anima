"""The combat loop must re-issue the attack request on a steady cadence.

A target adjacent (Chebyshev dist <= 1) the whole fight reaches neither the
focus-fire switch nor the gap-closing chase (both gated on dist > 1), so the
only 0x05 attack request it ever received was the engage send. If that single
request is dropped or raced (sent the same tick as war mode before the server
registered it, or the combatant cleared by a momentary LOS blip), nothing
re-arms it and the agent stands in war mode landing ZERO swings for the full
45s ENGAGEMENT_CAP_S. ``ATTACK_RESEND_S`` makes the loop re-send the attack for
a live adjacent target on a cadence so a stalled engagement self-heals.

Both ``asyncio.sleep`` and ``time.monotonic`` are mocked so the cadence is
driven by a deterministic fake clock, not wall time.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.client.packets import build_attack
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import ATTACK_RESEND_S, HuntNearby


def _mob(serial, x, y):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=50, hits_max=50,
    )


def _ctx(target):
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        stam=100, stam_max=100,
        equipment={1: 0xDEAD},          # weapon already in hand → no equip step
        skills={},                       # .get(...) → None → before/after = 0.0
        is_poisoned=False,
    )
    mobiles = {target.serial: target}

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(
        nearby_mobiles=nearby,
        mobiles=mobiles,
        items={},                        # no corpses
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


class _FakeClock:
    """Monotonic clock that advances a fixed step every asyncio.sleep tick."""

    def __init__(self, step=cl.TICK_S):
        self.t = 1000.0
        self.step = step

    def monotonic(self):
        return self.t

    async def sleep(self, _):
        self.t += self.step


@pytest.mark.asyncio
async def test_adjacent_target_gets_periodic_reattack(monkeypatch):
    # Target adjacent the whole fight (dist 1): never triggers switch/chase, so
    # without the keepalive the attack request is sent exactly once.
    target = _mob(0x2, 101, 100)
    ctx = _ctx(target)

    clock = _FakeClock(step=cl.TICK_S)
    monkeypatch.setattr(cl.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cl.asyncio, "sleep", clock.sleep)

    # Loot path also sleeps; keep it a no-op (no kills here anyway).
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    # No movement should happen for an adjacent target; pin go_to as a no-op.
    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))

    # Bail out of the loop after enough simulated time that several resend
    # windows have elapsed, by stopping the connection once the clock advances.
    real_sleep = clock.sleep

    async def sleep_then_maybe_stop(d):
        await real_sleep(d)
        # Run ~3 resend windows worth of ticks, then end the engagement.
        if clock.t - 1000.0 >= 3 * ATTACK_RESEND_S + cl.TICK_S:
            ctx.conn.connected = False

    monkeypatch.setattr(cl.asyncio, "sleep", sleep_then_maybe_stop)

    await HuntNearby().execute(ctx)

    attack_pkt = build_attack(target.serial)
    attack_sends = [
        c for c in ctx.conn.send_packet.await_args_list
        if c.args and c.args[0] == attack_pkt
    ]
    # Engage send + at least one cadence keepalive over ~3 resend windows.
    assert len(attack_sends) >= 2, (
        "an adjacent target must receive periodic attack-request keepalives, "
        f"got {len(attack_sends)} attack sends"
    )


@pytest.mark.asyncio
async def test_no_resend_inside_the_cadence_window(monkeypatch):
    # A very large TICK so a single loop iteration does NOT cross ATTACK_RESEND_S
    # would over-fire; conversely a tiny advance must NOT re-send. Here the clock
    # barely moves between ticks, so within the first cadence window only the
    # single engage attack should have been sent.
    target = _mob(0x2, 101, 100)
    ctx = _ctx(target)

    clock = _FakeClock(step=ATTACK_RESEND_S / 100.0)  # tiny advance per tick
    monkeypatch.setattr(cl.time, "monotonic", clock.monotonic)

    ticks = {"n": 0}

    async def sleep_then_stop(d):
        await clock.sleep(d)
        ticks["n"] += 1
        # Stop well before one full cadence window elapses.
        if ticks["n"] >= 5:
            ctx.conn.connected = False

    monkeypatch.setattr(cl.asyncio, "sleep", sleep_then_stop)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))

    await HuntNearby().execute(ctx)

    attack_pkt = build_attack(target.serial)
    attack_sends = [
        c for c in ctx.conn.send_packet.await_args_list
        if c.args and c.args[0] == attack_pkt
    ]
    # Inside one cadence window → only the engage send, no keepalive yet.
    assert len(attack_sends) == 1, (
        "no keepalive should fire inside the resend window; "
        f"got {len(attack_sends)} attack sends"
    )
