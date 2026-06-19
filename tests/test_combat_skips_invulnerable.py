"""Combat target acquisition must skip invulnerable (yellow-health) mobiles.

Regression: a Healer NPC (and town/protected NPCs generally) carries ServUO's
YELLOW_BAR flag — MobileInfo.is_yellow_health — and takes zero damage however
long the agent swings. _find_target filtered only on notoriety, so a healer that
reads as gray/ATTACKABLE notoriety was a valid candidate; focus-fire even sorts
it FIRST once its health bar reads non-full. The engagement loop then locks onto
it (it never dies and never leaves view), burning the entire 45s engagement cap
landing zero real kills. This is acute since the survival-arena Healer NPC was
co-located at the agent's resurrection / fight anchor (kernel commits K1+/K1/K3).

The fix excludes is_yellow_health mobiles from both _find_target (acquisition)
and _adjacent_hostiles (surround count). asyncio.sleep and time.monotonic are
mocked per the suite convention even though these selectors are synchronous, so
the test never touches the wall clock or the event loop.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.procedures.combat_loop import _adjacent_hostiles, _find_target


@pytest.fixture(autouse=True)
def _frozen_clock():
    # Suite convention: mock the clock + sleep so target-selection tests never
    # depend on wall time or yield to the loop.
    with patch("anima.procedures.combat_loop.time.monotonic", return_value=1000.0), \
         patch("anima.procedures.combat_loop.asyncio.sleep") as _sleep:
        yield _sleep


def _ctx(mobiles):
    ss = SimpleNamespace(x=100, y=100, serial=0x1)
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=0: list(mobiles)
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world)
    )


def test_find_target_skips_invulnerable_healer():
    # The healer is adjacent (closest) AND, once its bar reads non-full, has the
    # lowest hp_frac — so without the guard focus-fire would sort it first and
    # the agent would swing forever at an unkillable mob. The real Ettin is
    # farther away but is the only legitimate target.
    healer = MobileInfo(
        serial=0x10, body=0x0190, x=101, y=100,
        notoriety=NotorietyFlag.ATTACKABLE, hits_max=100, hits=100,
        is_yellow_health=True,
    )
    ettin = MobileInfo(
        serial=0x11, body=0x0021, x=105, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=180,
    )
    target = _find_target(_ctx([healer, ettin]))
    assert target is not None
    assert target.serial == 0x11, "must pick the killable Ettin, not the healer"


def test_find_target_skips_invulnerable_even_when_lowest_hp():
    # Focus-fire sorts lowest hp_frac first; an invulnerable mob reading wounded
    # would otherwise be selected ahead of a healthy killable one.
    healer = MobileInfo(
        serial=0x10, body=0x0190, x=101, y=100,
        notoriety=NotorietyFlag.CRIMINAL, hits_max=100, hits=5,
        is_yellow_health=True,
    )
    killable = MobileInfo(
        serial=0x11, body=0x0021, x=102, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=200,
    )
    target = _find_target(_ctx([healer, killable]))
    assert target is not None
    assert target.serial == 0x11


def test_find_target_returns_none_when_only_invulnerable():
    healer = MobileInfo(
        serial=0x10, body=0x0190, x=101, y=100,
        notoriety=NotorietyFlag.ATTACKABLE, hits_max=100, hits=100,
        is_yellow_health=True,
    )
    assert _find_target(_ctx([healer])) is None


def test_find_target_still_picks_normal_hostile():
    # A normal (non-yellow) hostile must remain selectable — the guard must not
    # over-filter the common case.
    mob = MobileInfo(
        serial=0x10, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=42,
    )
    target = _find_target(_ctx([mob]))
    assert target is not None
    assert target.serial == 0x10


def test_adjacent_hostiles_excludes_invulnerable():
    # An adjacent invulnerable healer must not inflate the surround count (and so
    # must not trigger an anti-surround reposition away from real threats).
    healer = MobileInfo(
        serial=0x10, body=0x0190, x=100, y=101,
        notoriety=NotorietyFlag.ATTACKABLE, hits_max=100, hits=100,
        is_yellow_health=True,
    )
    ettin = MobileInfo(
        serial=0x11, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=200,
    )
    adjacent = _adjacent_hostiles(_ctx([healer, ettin]))
    serials = {m.serial for m in adjacent}
    assert serials == {0x11}, "healer must be excluded from the surround count"
