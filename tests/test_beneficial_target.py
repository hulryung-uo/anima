"""find_beneficial_target: friend/foe gate + self-vs-other correctness.

A beneficial action (heal/bless an ally) is the inverse of combat targeting.
These pin the three failure modes the selector exists to prevent:
  * targeting a hostile/gray mobile (would flag the caller criminal),
  * confusing self and another mobile (wrong serial on the cursor),
  * wasting the action on a full-HP or dead target.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.procedures.beneficial import find_beneficial_target


def _ctx(mobiles, *, self_hits=100, self_hits_max=100, self_serial=0x1):
    ss = SimpleNamespace(
        x=100, y=100, serial=self_serial,
        hits=self_hits, hits_max=self_hits_max,
    )
    world = SimpleNamespace(
        # Mirror the real Chebyshev box filter so the candidate set matches
        # production rather than an everything-passes stub.
        nearby_mobiles=lambda x, y, distance=0: [
            m for m in mobiles
            if abs(m.x - x) <= distance and abs(m.y - y) <= distance
        ]
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def _ally(serial, hits, hits_max=100, x=102, y=100, noto=NotorietyFlag.ALLY):
    return MobileInfo(
        serial=serial, body=0x0190, x=x, y=y,
        notoriety=noto, hits_max=hits_max, hits=hits,
    )


def test_heals_wounded_ally_not_self_when_self_full():
    # Self at full HP, a wounded green ally nearby -> target the ally's serial.
    ally = _ally(0x10, hits=40)
    out = find_beneficial_target(_ctx([ally]))
    assert out == 0x10


def test_never_targets_a_hostile_even_if_badly_wounded():
    # A near-dead ENEMY is exactly the mobile combat would focus-fire; a
    # beneficial action must NOT touch it (healing it flags us / is refused).
    enemy = _ally(0x20, hits=1, noto=NotorietyFlag.ENEMY)
    out = find_beneficial_target(_ctx([enemy]))  # self is full HP
    assert out is None, "must not heal a hostile mobile"


def test_never_targets_gray_or_criminal():
    for noto in (NotorietyFlag.ATTACKABLE, NotorietyFlag.CRIMINAL,
                 NotorietyFlag.MURDERER, NotorietyFlag.INVULNERABLE):
        m = _ally(0x21, hits=10, noto=noto)
        assert find_beneficial_target(_ctx([m])) is None, f"noto={noto} must be skipped"


def test_innocent_blue_is_a_valid_recipient():
    blue = _ally(0x30, hits=50, noto=NotorietyFlag.INNOCENT)
    assert find_beneficial_target(_ctx([blue])) == 0x30


def test_self_wins_when_self_is_more_wounded_than_ally():
    # Self at 20% HP, ally at 60% -> heal self (returns self's serial), not the
    # ally. This is the self-vs-other confusion guard.
    ally = _ally(0x40, hits=60)
    out = find_beneficial_target(_ctx([ally], self_hits=20, self_serial=0xABC))
    assert out == 0xABC


def test_ally_wins_only_when_strictly_more_wounded():
    # Self 50%, ally 50% -> tie goes to self (in hand). Ally 49% -> ally wins.
    tie_ally = _ally(0x50, hits=50)
    assert find_beneficial_target(_ctx([tie_ally], self_hits=50, self_serial=0x9)) == 0x9
    worse_ally = _ally(0x50, hits=49)
    assert find_beneficial_target(_ctx([worse_ally], self_hits=50, self_serial=0x9)) == 0x50


def test_picks_most_wounded_of_several_allies_order_independent():
    a = _ally(0x60, hits=80)
    b = _ally(0x61, hits=10)   # worst off
    c = _ally(0x62, hits=55)
    assert find_beneficial_target(_ctx([a, b, c])) == 0x61
    # Reversed iteration order must give the same answer.
    assert find_beneficial_target(_ctx([c, b, a])) == 0x61


def test_skips_full_hp_and_dead_allies():
    full = _ally(0x70, hits=100)            # nothing to heal
    ghost = MobileInfo(serial=0x71, body=0x0192, x=101, y=100,  # ghost body
                       notoriety=NotorietyFlag.ALLY, hits_max=100, hits=0)
    out = find_beneficial_target(_ctx([full, ghost]))  # self full too
    assert out is None


def test_unknown_health_bar_is_not_targeted():
    # hits_max == 0 means we never queried the bar; can't claim it's wounded.
    unknown = _ally(0x80, hits=0, hits_max=0)
    assert find_beneficial_target(_ctx([unknown])) is None


def test_returns_none_when_nobody_needs_healing():
    assert find_beneficial_target(_ctx([], self_hits=100)) is None


def test_self_listed_in_world_is_not_double_counted():
    # If self also appears in nearby_mobiles, it must be skipped there (the
    # self_state is authoritative) and not produce a bogus second candidate.
    self_serial = 0x1
    self_in_world = MobileInfo(
        serial=self_serial, body=0x0190, x=100, y=100,
        notoriety=NotorietyFlag.INNOCENT, hits_max=100, hits=30,
    )
    out = find_beneficial_target(_ctx([self_in_world], self_hits=30,
                                      self_serial=self_serial))
    assert out == self_serial
