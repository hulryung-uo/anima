"""_find_target nearest-tiebreak must use Chebyshev (UO step) distance.

Regression: the focus-fire sort key measured distance with Manhattan
(``abs(dx) + abs(dy)``) while the rest of the world-state spatial layer ranges
on Chebyshev (``max(abs(dx), abs(dy))``) — the box filter in
``WorldState.nearby_mobiles``, ``craft_blacksmith._find_nearest`` and
``world_knowledge.nearest_locations`` all agree on Chebyshev because a diagonal
move costs one UO step. With two equally-wounded targets the Manhattan key could
select the mob that is strictly farther to walk to (more steps to close), the
opposite of "engage the nearest". This pins the metric to Chebyshev.
"""
from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo
from anima.procedures.combat_loop import _find_target


def _ctx(mobiles):
    ss = SimpleNamespace(x=100, y=100, serial=0x1)
    world = SimpleNamespace(
        # Mirror the real box (Chebyshev) filter so the candidate set matches
        # production, not an everything-passes stub.
        nearby_mobiles=lambda x, y, distance=0: [
            m for m in mobiles
            if abs(m.x - x) <= distance and abs(m.y - y) <= distance
        ]
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world)
    )


def test_find_target_prefers_fewer_steps_diagonal():
    # Self at (100,100). Both mobs are equally wounded so distance decides.
    #   near : (104,104) -> Chebyshev 4 (4 diagonal steps),  Manhattan 8
    #   far  : (106,101) -> Chebyshev 6 (6 steps to close),  Manhattan 7
    # Manhattan would pick `far` (7 < 8); Chebyshev correctly picks `near`,
    # which is genuinely fewer steps to reach. Both are inside ENGAGE_RANGE=10.
    near = MobileInfo(
        serial=0x10, body=0x0021, x=104, y=104,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=25,
    )
    far = MobileInfo(
        serial=0x11, body=0x0021, x=106, y=101,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=25,
    )
    target = _find_target(_ctx([near, far]))
    assert target is not None
    assert target.serial == 0x10, "should engage the mob that is fewer UO steps away"


def test_find_target_chebyshev_is_order_independent():
    # Same scenario, candidate list reversed — selection must be stable and
    # still pick the Chebyshev-nearest mob regardless of dict/iteration order.
    near = MobileInfo(
        serial=0x10, body=0x0021, x=104, y=104,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=25,
    )
    far = MobileInfo(
        serial=0x11, body=0x0021, x=106, y=101,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=25,
    )
    target = _find_target(_ctx([far, near]))
    assert target is not None
    assert target.serial == 0x10


def test_find_target_focus_fire_still_beats_distance():
    # Health priority must remain primary: the wounded mob wins even when it is
    # the Chebyshev-farther of the two (distance is only the tiebreaker).
    far_hurt = MobileInfo(
        serial=0x20, body=0x0021, x=108, y=108,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=5,
    )
    near_healthy = MobileInfo(
        serial=0x21, body=0x0021, x=101, y=100,
        notoriety=NotorietyFlag.ENEMY, hits_max=50, hits=50,
    )
    target = _find_target(_ctx([far_hurt, near_healthy]))
    assert target is not None
    assert target.serial == 0x20, "lowest-HP target should still sort first"
