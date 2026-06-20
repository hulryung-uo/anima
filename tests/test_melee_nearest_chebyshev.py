"""MeleeAttack (behavior-tree path) ranks foes by Chebyshev distance.

Regression: anima/skills/combat/melee.py._find_target sorted candidates by
Manhattan distance (|dx| + |dy|), which double-counts diagonals. UO — and the
procedures-path twin combat_loop._find_target (combat_loop.py:673) — range on
Chebyshev distance (max(|dx|, |dy|)). With Manhattan a genuinely closer diagonal
foe was ranked behind a farther axis-aligned one, so MeleeAttack engaged the
farther target, lengthening the approach and the time exposed in combat.

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


def test_diagonal_foe_outranks_axis_foe():
    # Self at (100,100).
    #   diag : (103,103) -> Chebyshev 3, Manhattan 6
    #   axis : (104,100) -> Chebyshev 4, Manhattan 4
    # The diagonal foe is genuinely closer (3 tiles). Manhattan would pick the
    # axis foe (4 < 6); Chebyshev correctly picks the diagonal foe (3 < 4).
    diag = MobileInfo(
        serial=0xD1A6, body=0x0021, x=103, y=103,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=180,
    )
    axis = MobileInfo(
        serial=0xA415, body=0x0021, x=104, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=180,
    )
    # Order in the world list must not matter — the sort decides.
    for ordered in ([diag, axis], [axis, diag]):
        target = _find_target(_ctx(ordered))
        assert target is not None
        assert target.serial == 0xD1A6, (
            "nearest by Chebyshev (the diagonal foe) must be chosen"
        )


def test_axis_foe_chosen_when_truly_nearest():
    # Sanity: when the axis foe IS the nearest by Chebyshev it is still chosen,
    # so the new metric does not invert the common case.
    near_axis = MobileInfo(
        serial=0x0001, body=0x0021, x=102, y=100,  # Chebyshev 2
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=180,
    )
    far_diag = MobileInfo(
        serial=0x0002, body=0x0021, x=105, y=105,  # Chebyshev 5
        notoriety=NotorietyFlag.ENEMY, hits_max=200, hits=180,
    )
    target = _find_target(_ctx([far_diag, near_axis]))
    assert target is not None
    assert target.serial == 0x0001
