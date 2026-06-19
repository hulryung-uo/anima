"""MeleeAttack (behavior-tree path) target acquisition skips invulnerable mobs.

Regression: the procedures path (combat_loop._find_target) excludes
is_yellow_health mobiles — a Healer/town NPC carries ServUO's YELLOW_BAR flag
and takes zero damage however long the agent swings. The BT-path skill
anima/skills/combat/melee.py._find_target filtered only on notoriety, so a
healer reading gray/ATTACKABLE notoriety was a valid candidate. MeleeAttack
would lock onto the immortal healer, burn the full COMBAT_TIMEOUT, and return
success=False with a -5 reward — polluting the Q-learning signal. This is acute
since the survival-arena Healer NPC was co-located at the resurrection / fight
anchor (kernel commits K1+/K1/K3).

asyncio.sleep / time.monotonic are mocked per the suite convention even though
_find_target is synchronous, so the test never touches the wall clock or loop.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.skills.combat.melee import _find_target


@pytest.fixture(autouse=True)
def _frozen_clock():
    with patch("anima.skills.combat.melee.time.monotonic", return_value=1000.0), \
         patch("anima.skills.combat.melee.asyncio.sleep") as _sleep:
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
    # The healer is closest (distance 1) so without the guard it would be
    # selected ahead of the real, farther Ettin and the engage loop would lock
    # onto an unkillable mob for the whole COMBAT_TIMEOUT.
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


def test_find_target_returns_none_when_only_invulnerable():
    healer = MobileInfo(
        serial=0x10, body=0x0190, x=101, y=100,
        notoriety=NotorietyFlag.CRIMINAL, hits_max=100, hits=5,
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


def test_find_target_ignores_missing_yellow_field():
    # SimpleNamespace mobs that never define is_yellow_health must NOT be
    # mis-excluded (`is True`, not truthy).
    mob = SimpleNamespace(
        serial=0x10, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=42,
    )
    target = _find_target(_ctx([mob]))
    assert target is not None
    assert target.serial == 0x10
