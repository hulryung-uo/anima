"""Profession-loop precedence: the adventurer loot-flush (sell_to_vendor) must
not abandon a live fight.

The loot-flush gate fires once >= LOOT_SELL_BACKLOG_THRESHOLD vendor-sellable
items pile up in the pack and preempts the ENTIRE first-startable profession
loop — hunt_nearby included. In the survival-arena layout the vendor is
co-located with the spawn, so sell_to_vendor.can_start succeeds mid-combat and
the warrior walks away from an engaged mob to sell, every loop, forfeiting
swings and turning its back on the swarm. ``_loot_flush_should_yield_to_combat``
makes the flush defer to an in-range combat target so loot only flushes BETWEEN
fights.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import _loot_flush_should_yield_to_combat


def _ss(hits=100, hits_max=100):
    return SimpleNamespace(
        x=100, y=100, serial=0x1,
        hits=hits, hits_max=hits_max,
        hp_percent=(hits / hits_max) * 100.0 if hits_max else 100.0,
    )


def _mob(serial, x, y, notoriety=NotorietyFlag.ENEMY, body=0x10):
    return SimpleNamespace(
        serial=serial, x=x, y=y, notoriety=notoriety, body=body, is_dead=False,
    )


def _ctx(ss, mobiles):
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=18: [
            m for m in mobiles
            if max(abs(m.x - x), abs(m.y - y)) <= distance
        ],
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def test_yields_when_target_in_engage_range():
    # A live enemy adjacent: the loot-flush must defer so hunt_nearby keeps the
    # fight instead of walking off to sell.
    ss = _ss()
    ctx = _ctx(ss, [_mob(0x2, 101, 100)])
    assert _loot_flush_should_yield_to_combat(ctx) is True


def test_does_not_yield_when_no_target_in_range():
    # No fight available — flush the loot backlog to the vendor.
    ss = _ss()
    ctx = _ctx(ss, [])
    assert _loot_flush_should_yield_to_combat(ctx) is False


def test_does_not_yield_when_target_out_of_engage_range():
    # A mob well outside ENGAGE_RANGE (10) is not a fight to keep — flush.
    ss = _ss()
    ctx = _ctx(ss, [_mob(0x2, 100, 116)])  # 16 tiles away
    assert _loot_flush_should_yield_to_combat(ctx) is False


def test_does_not_yield_when_only_dead_bodies_in_range():
    # A just-felled mob lingers in world; _find_target drops corpses, so the
    # fight is over and the loot should flush.
    ss = _ss()
    corpse = _mob(0x2, 101, 100)
    corpse.is_dead = True
    ctx = _ctx(ss, [corpse])
    assert _loot_flush_should_yield_to_combat(ctx) is False


def test_does_not_yield_to_a_bare_gray_human():
    # A bare-gray ATTACKABLE human is not a foe (_find_target excludes it) — no
    # fight to keep, so flush the loot.
    ss = _ss()
    gray = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ATTACKABLE, body=0x0190)
    ctx = _ctx(ss, [gray])
    assert _loot_flush_should_yield_to_combat(ctx) is False
