"""Planner huntable-target selection must rank prey by Chebyshev step-cost.

UO movement is 8-directional: a diagonal step advances one tile on BOTH axes
at once, so the real walk cost between two tiles is the Chebyshev distance
``max(|dx|, |dy|)``, not the Manhattan sum ``|dx| + |dy|``. The combat loop's
target scan (``combat_loop._find_target``) was already fixed to rank on
Chebyshev (commit 83b35d2), and ``world.nearby_mobiles`` uses a Chebyshev box;
the planner's ``_find_huntable_target`` candidate sort was the last place still
ranking on Manhattan, so it could send the agent to walk to a farther mob (in
real step cost) over a nearer one.

Regression guard: with a foe 5 tiles diagonally away (5 steps) and another 8
tiles due-south (8 steps), Manhattan ranks the southern foe nearer (8 < 10) but
Chebyshev correctly ranks the diagonal foe nearer (5 < 8). The hunt scan must
pick the diagonal (fewest-steps) foe.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _foe(serial: int, x: int, y: int):
    # A genuine live foe: hostile notoriety, non-human body, alive, not blessed.
    return SimpleNamespace(
        serial=serial,
        notoriety=NotorietyFlag.MURDERER,
        body=0x0021,
        is_yellow_health=False,
        x=x,
        y=y,
    )


def _make_ctx(mobiles):
    ss = SimpleNamespace(
        serial=0x1, x=100, y=200, z=0,
        equipment={0x15: 0x101},
    )
    perception = SimpleNamespace(
        self_state=ss,
        world=SimpleNamespace(
            nearby_mobiles=lambda x, y, distance: list(mobiles),
        ),
    )
    # map_reader falsy -> _find_huntable_target returns candidates[0] (the
    # ranked-nearest mob) without the pathfinding reachability filter.
    return SimpleNamespace(
        perception=perception,
        blackboard={},
        map_reader=None,
    )


def test_huntable_target_ranks_by_chebyshev_not_manhattan():
    planner = Planner(ProcedureRegistry())

    # Diagonal foe: dx=5, dy=5 -> Chebyshev 5 steps, Manhattan 10.
    diagonal = _foe(0xA, 105, 205)
    # Due-south foe: dx=0, dy=8 -> Chebyshev 8 steps, Manhattan 8.
    south = _foe(0xB, 100, 208)

    # Insert the Manhattan-nearer foe FIRST so a stable sort can't accidentally
    # produce the right answer for the wrong reason.
    ctx = _make_ctx([south, diagonal])
    ss = ctx.perception.self_state

    target = planner._find_huntable_target(ctx, ss)

    # The diagonal foe is genuinely fewer walk-steps (5 < 8). Manhattan ranking
    # would have wrongly returned the due-south foe (8 < 10).
    assert target is diagonal


def test_huntable_target_diagonal_tie_breaks_correctly():
    planner = Planner(ProcedureRegistry())

    # Both foes are exactly Chebyshev-equal at 6 steps, but the SE foe has a
    # larger Manhattan sum (12 vs 6). Chebyshev treats them as equidistant;
    # the agent should never be steered away from the genuinely-as-close foe.
    pure_axis = _foe(0xC, 100, 206)   # dx=0, dy=6 -> Cheb 6, Manh 6
    diagonal = _foe(0xD, 106, 206)    # dx=6, dy=6 -> Cheb 6, Manh 12

    ctx = _make_ctx([diagonal, pure_axis])
    ss = ctx.perception.self_state

    target = planner._find_huntable_target(ctx, ss)

    # Stable sort keeps the first equal-key element (the diagonal foe, inserted
    # first). The point is that Chebyshev does NOT demote the diagonal foe below
    # the axis foe the way Manhattan (12 > 6) would.
    assert target is diagonal
