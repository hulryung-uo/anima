"""Combat target selection must skip dead mobiles.

Regression: a just-killed mob lingers in world.mobiles (known health bar at
zero, or a ghost body) until its 0x1D Delete arrives. _find_target's focus-fire
sorts the lowest-HP mob first, so the corpse (hp_frac == 0) sorted to the very
top and the agent re-attacked the body it had just felled instead of finding a
live target. The fix makes MobileInfo.is_dead authoritative (ghost body OR a
KNOWN health bar at zero) and skips dead mobiles in _find_target.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.procedures.combat_loop import _find_target


# ---------------------------------------------------------------------------
# MobileInfo.is_dead — perception-layer death predicate
# ---------------------------------------------------------------------------


def test_is_dead_ghost_body():
    assert MobileInfo(serial=1, body=0x0192).is_dead is True
    assert MobileInfo(serial=1, body=0x0193).is_dead is True


def test_is_dead_mounted_ghost_body():
    # A mobile that dies while mounted/flying turns into one of these ghost
    # graphics, NOT 0x0192/0x0193. ClassicUO Mobile.IsDead recognizes them;
    # MobileInfo.is_dead must too, or a mounted enemy's corpse keeps counting
    # as a live hostile. (Ref: ClassicUO Game/GameObjects/Mobile.cs IsDead.)
    for body in (0x025F, 0x0260, 0x02B6, 0x02B7):
        assert MobileInfo(serial=1, body=body).is_dead is True, f"0x{body:04X}"


def test_is_dead_living_body_near_ghost_range_is_alive():
    # A live body id just outside the mounted-ghost range must NOT be dead.
    assert MobileInfo(serial=1, body=0x0261, hits_max=50, hits=37).is_dead is False
    assert MobileInfo(serial=1, body=0x025E, hits_max=50, hits=37).is_dead is False


def test_is_dead_known_health_bar_at_zero():
    # Known bar (hits_max > 0) reading zero == dead.
    m = MobileInfo(serial=1, body=0x0021, hits_max=50, hits=0)
    assert m.is_dead is True


def test_is_dead_unqueried_mob_is_not_dead():
    # Never-inspected mob defaults hits/hits_max to 0 — must NOT count as dead,
    # otherwise every un-inspected mob looks like a corpse.
    m = MobileInfo(serial=1, body=0x0021, hits_max=0, hits=0)
    assert m.is_dead is False


def test_is_dead_alive_mob():
    m = MobileInfo(serial=1, body=0x0021, hits_max=50, hits=37)
    assert m.is_dead is False


# ---------------------------------------------------------------------------
# _find_target — must not re-select a dead mob
# ---------------------------------------------------------------------------


def _ctx(mobiles):
    ss = SimpleNamespace(x=100, y=100, serial=0x1)
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=0: list(mobiles)
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world)
    )


def test_find_target_skips_freshly_killed_mob():
    # The corpse is adjacent (closest) AND has hp_frac == 0, so without the
    # dead-filter focus-fire would sort it first and the agent would re-attack
    # the body it just felled. The live mob is farther away.
    dead = MobileInfo(
        serial=0x10, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=0,
    )
    alive = MobileInfo(
        serial=0x11, body=0x0021, x=105, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=42,
    )
    target = _find_target(_ctx([dead, alive]))
    assert target is not None
    assert target.serial == 0x11, "should pick the live mob, not the corpse"


def test_find_target_skips_ghost_body_mob():
    ghost = MobileInfo(
        serial=0x10, body=0x0192, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY,
    )
    alive = MobileInfo(
        serial=0x11, body=0x0021, x=105, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=42,
    )
    target = _find_target(_ctx([ghost, alive]))
    assert target is not None
    assert target.serial == 0x11


def test_find_target_returns_none_when_only_dead_mobs():
    dead = MobileInfo(
        serial=0x10, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=0,
    )
    assert _find_target(_ctx([dead])) is None


def test_find_target_still_picks_unqueried_live_mob():
    # Un-inspected mob (hits_max == 0) is NOT dead and must remain selectable —
    # this is the common case before any health bar round-trip.
    fresh = MobileInfo(
        serial=0x10, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=0, hits=0,
    )
    target = _find_target(_ctx([fresh]))
    assert target is not None
    assert target.serial == 0x10
