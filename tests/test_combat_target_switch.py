"""Focus-fire must re-evaluate before chasing — don't over-commit to a fleer.

The melee loop picks a target once and only re-runs ``_find_target`` when *that*
mob dies. A fast kiter (or one that pathed out of melee) would otherwise drag
the agent across the map while a closer, reachable hostile stands adjacent and
swings freely. This test pins the fix: before each chase leg the loop consults
``_find_target`` and, if it names a *different* live candidate strictly closer
than the current target, switches the swing to it — and only chases the closer
one.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby
from anima.client.packets import build_attack


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=hits, hits_max=hits_max,
    )


def _ctx(mobiles):
    ss = SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=100, hits_max=100, hp_percent=100.0, is_alive=True,
        stam=100, stam_max=100,
        equipment={1: 0xDEAD},
        skills={},
    )

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(
        nearby_mobiles=nearby,
        mobiles=mobiles,
        items={},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_switches_to_closer_target_instead_of_chasing_fleer(monkeypatch):
    # At fight start the fleer (0x2) is the only hostile, so the loop commits to
    # it. By the first chase tick the fleer has run to 6 tiles away AND a fresh,
    # closer hostile (0x3) has appeared at 2 tiles — strictly closer, also full
    # HP. The loop must re-evaluate and chase the closer one, NOT keep running
    # the fleer down. Without the re-evaluation the loop chases 0x2 to (106,100).
    fleer = _mob(0x2, 101, 100)            # adjacent-ish at start → becomes target
    mobiles: dict[int, object] = {fleer.serial: fleer}
    ctx = _ctx(mobiles)

    go_to_dests: list[tuple[int, int]] = []
    closer = _mob(0x3, 102, 100)

    async def fake_go_to(c, x, y, run=None, interrupt_check=None, **kw):
        go_to_dests.append((x, y))
        # On reaching the chased mob, kill it and clear the field so the loop
        # exits promptly after one chase.
        for m in list(mobiles.values()):
            if (m.x, m.y) == (x, y):
                m.hits = 0
                m.is_dead = True
        c.perception.self_state.x, c.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(movement, "go_to", fake_go_to)
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))

    tick = {"n": 0}

    async def _sleep_and_evolve(_):
        # After the loop's first per-tick sleep: the fleer has bolted out to 6
        # tiles and a closer hostile has spawned at 2 tiles.
        tick["n"] += 1
        if tick["n"] == 1:
            fleer.x = 106
            mobiles[closer.serial] = closer
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _sleep_and_evolve)

    result = await HuntNearby().execute(ctx)

    # The FIRST chase must go to the closer mob (102,100), not the distant
    # fleer (106,100). Once that mob is dead the loop legitimately re-targets
    # the fleer, so we only pin the initial chase ordering here.
    assert go_to_dests, "a chase leg should have run"
    assert go_to_dests[0] == (102, 100), (
        "first chase must switch to the closer hostile, not run down the fleer"
    )

    # And it sent an attack at the closer mob's serial after switching.
    attack_payload = build_attack(closer.serial)
    sent = [c.args[0] for c in ctx.conn.send_packet.call_args_list]
    assert attack_payload in sent, "must re-issue attack on the switched target"
    assert result.success is True
