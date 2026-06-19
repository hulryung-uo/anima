"""A focus-fire target SWITCH must re-baseline the kill-confirmation tracking.

When the hunt loop switches its swing to a closer hostile (the focus-fire
re-evaluation, commit b8630d8) it changes ``target``/``current`` but historically
left the kill-confirmation scratch state pointed at the OLD target: the last-seen
health bar (``last_target_hits``) and the engage-start corpse snapshot
(``known_corpses``). If the freshly-switched target then kited out of view on the
next tick, ``_confirm_kill`` judged it against stale data — and any corpse that
dropped *after* the original engagement started (e.g. an earlier kill's unlooted
full-pack body) counted as "new" evidence, crediting a PHANTOM kill and firing a
bogus loot pass. That re-opens, on the switch path, the very stale-corpse
false-confirm hole the re-pick snapshot (commit 5195230) closes.

This test pins the fix: after a switch, a kiter leaving view over a corpse that
appeared *during* the engagement is NOT credited as a kill.

asyncio.sleep and time.monotonic are mocked so the loop terminates instantly.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.action.movement as movement
import anima.procedures.combat_loop as cl
from anima.perception.enums import NotorietyFlag
from anima.procedures.combat_loop import HuntNearby


def _mob(serial, x, y, hits=50, hits_max=50):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=NotorietyFlag.ENEMY,
        body=0x0021, is_dead=False, hits=hits, hits_max=hits_max,
    )


def _corpse(serial, x, y):
    return SimpleNamespace(serial=serial, x=x, y=y, container=0, graphic=0x2006)


def _ctx(mobiles, items):
    class _SS:
        x = 100
        y = 100
        serial = 0x1
        hits = 100
        hits_max = 100
        stam = 100
        stam_max = 100
        equipment = {1: 0xDEAD}
        skills: dict = {}

        @property
        def hp_percent(self):
            return (self.hits / self.hits_max) * 100.0 if self.hits_max else 100.0

        @property
        def is_alive(self):
            return self.hits > 0 or self.hits_max == 0

    ss = _SS()

    def nearby(x, y, distance=18):
        return [m for m in mobiles.values()
                if abs(m.x - x) <= distance and abs(m.y - y) <= distance]

    world = SimpleNamespace(nearby_mobiles=nearby, mobiles=mobiles, items=items)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        conn=SimpleNamespace(connected=True, send_packet=AsyncMock()),
    )


def _freeze_clock(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(cl.time, "monotonic", lambda: clock["t"])
    return clock


@pytest.mark.asyncio
async def test_switch_then_kiter_over_engagement_corpse_is_not_a_kill(monkeypatch):
    # Engagement opens on the fleer (0x2) at dist 2; no corpse on the ground yet,
    # so the engage-start snapshot is empty. On tick 1 a closer hostile (0x3,
    # dist 1) appears AND a corpse (0xBEEF) drops onto the ground — the loop's
    # focus-fire re-evaluation switches the swing to the closer mob. On tick 2
    # that switched target kites out of view with NO new corpse appearing. The
    # corpse that dropped *after* engage-start must NOT confirm a kill: without
    # re-snapshotting known_corpses on the switch, _confirm_kill would treat it
    # as fresh evidence and credit a phantom kill (+ a bogus loot pass).
    fleer = _mob(0x2, 102, 100, hits=50, hits_max=50)
    closer = _mob(0x3, 101, 100, hits=50, hits_max=50)
    mobiles = {fleer.serial: fleer}
    items: dict = {}
    ctx = _ctx(mobiles, items)

    # No chase actually relocates anyone in this test; go_to is a no-op.
    monkeypatch.setattr(movement, "go_to", AsyncMock(return_value=True))
    monkeypatch.setattr(cl, "equip_shield_from_pack", AsyncMock(
        return_value=SimpleNamespace(success=False, data=None)))
    loot_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(cl, "_loot_fresh_corpses", loot_mock)

    clock = _freeze_clock(monkeypatch)
    ticks = {"n": 0}

    async def _sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 1:
            # A closer hostile shows up and a body drops on the ground.
            mobiles[closer.serial] = closer
            items[0xBEEF] = _corpse(0xBEEF, 101, 100)
        elif ticks["n"] == 2:
            # The just-switched-to target kites out of view; no NEW corpse.
            mobiles.pop(closer.serial, None)
        elif ticks["n"] == 3:
            # Clear the field so the loop terminates promptly after re-pick.
            mobiles.pop(fleer.serial, None)
        clock["t"] += cl.ENGAGEMENT_CAP_S / 4.0
        return None

    monkeypatch.setattr(cl.asyncio, "sleep", _sleep)

    result = await HuntNearby().execute(ctx)

    # The switched target left view over a corpse that was NOT on the ground at
    # engage start — but it dropped during the engagement, so re-snapshotting at
    # the switch makes it pre-existing for the new target: NOT a kill, no loot.
    assert "0 kills" in result.message, result.message
    loot_mock.assert_not_awaited()
