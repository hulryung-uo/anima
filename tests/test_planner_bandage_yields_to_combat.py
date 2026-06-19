"""Profession-loop selection: a standalone bandage_self must not preempt
hunt_nearby when a hostile is in range and the warrior is only lightly wounded.

``bandage_self.can_start`` admits any wound below 95% HP, and the adventurer
PROFESSION_LOOPS leads with bandage_self (first-startable-wins). Without a
guard, a warrior at 94% HP with a mob adjacent would bandage every loop instead
of swinging — the COMBAT-uptime leak behind the engaged-vs-idle seed variance.
``_bandage_should_yield_to_combat`` makes the standalone bandage defer to the
fight (which heals itself via the interleaved _maybe_bandage at 85%) while a
target is in range and HP is above the retreat floor.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import _bandage_should_yield_to_combat
from anima.procedures.combat_loop import RETREAT_HP_PCT


def _ss(hits, hits_max=100):
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


def test_yields_when_lightly_wounded_with_target_in_range():
    # 94% HP (bandage_self.can_start would say yes) + a mob adjacent: the
    # standalone bandage must yield so hunt_nearby leads instead.
    ss = _ss(94)
    ctx = _ctx(ss, [_mob(0x2, 101, 100)])
    assert _bandage_should_yield_to_combat(ctx, ss) is True


def test_does_not_yield_when_no_target_in_range():
    # No fight available: keep the standalone bandage so the agent heals up
    # BETWEEN fights instead of wandering wounded.
    ss = _ss(60)
    ctx = _ctx(ss, [])
    assert _bandage_should_yield_to_combat(ctx, ss) is False


def test_does_not_yield_when_critically_wounded():
    # At/under the combat retreat floor we are in real danger — do not defer to
    # the fight (Priority-1 heal-in-place owns this band anyway).
    ss = _ss(int(RETREAT_HP_PCT) - 1)  # below 35%
    ctx = _ctx(ss, [_mob(0x2, 101, 100)])
    assert _bandage_should_yield_to_combat(ctx, ss) is False


def test_does_not_yield_when_only_dead_bodies_in_range():
    # A just-felled mob lingers in world.mobiles; _find_target drops corpses, so
    # the fight is actually over and the bandage should NOT yield (heal up).
    ss = _ss(70)
    corpse = _mob(0x2, 101, 100)
    corpse.is_dead = True
    ctx = _ctx(ss, [corpse])
    assert _bandage_should_yield_to_combat(ctx, ss) is False


def test_does_not_yield_to_a_bare_gray_human():
    # A bare-gray ATTACKABLE human is not a foe the warrior swings at
    # (_find_target excludes it), so there is no fight to keep — don't yield.
    ss = _ss(70)
    gray = _mob(0x2, 101, 100, notoriety=NotorietyFlag.ATTACKABLE, body=0x0190)
    ctx = _ctx(ss, [gray])
    assert _bandage_should_yield_to_combat(ctx, ss) is False
