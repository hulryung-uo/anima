"""CraftCarpentry.can_execute must agree with _pick_target (the craft selector).

Regression: can_execute gated on a static ``materials < 4`` threshold while
execute() picks what to craft via ``_pick_target(skill, boards)``. The two
disagreed for a *fresh* carpenter: the cheapest recipe a skill-0.0 carpenter
qualifies for is Barrel Staves (5 boards), so a skill-0 carpenter holding
exactly 4 boards passed can_execute (4 >= 4) but ``_pick_target(0.0, 4)``
returns None. The planner would then keep selecting the "ready" skill and
execute() fell straight into the "need more wood" shortage branch, returning
reward=-0.5 every cycle — a guaranteed failure poisoning the carpentry signal.

can_execute now defers to _pick_target, so it is only ready when an item is
genuinely craftable with the boards + skill on hand.
"""
from types import SimpleNamespace

import pytest

from anima.skills.crafting.carpentry import (
    CARPENTRY_SKILL_ID,
    CraftCarpentry,
)

_BACKPACK = 0x40000015
_BOARD_GRAPHIC = 0x1BD7


def _ctx(skill_val: float, board_amount: int):
    """Build a ctx whose backpack holds a saw + a board stack."""
    items = {
        0x100: SimpleNamespace(
            container=_BACKPACK, graphic=0x1034, amount=1, hue=0,  # saw
        ),
        0x101: SimpleNamespace(
            container=_BACKPACK, graphic=_BOARD_GRAPHIC,
            amount=board_amount, hue=0,
        ),
    }
    ss = SimpleNamespace(
        equipment={0x15: _BACKPACK},
        weight=100,
        weight_max=400,
        skills={CARPENTRY_SKILL_ID: SimpleNamespace(value=skill_val, lock=0)},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items=items),
        ),
        blackboard={},
    )


@pytest.mark.asyncio
async def test_fresh_carpenter_four_boards_not_ready():
    # Skill 0.0, exactly 4 boards: the only qualifying recipe (Barrel Staves)
    # needs 5 boards, so nothing is craftable. Before the fix can_execute
    # returned True (4 >= 4) and execute() immediately failed with -0.5.
    ctx = _ctx(0.0, 4)
    # Sanity: the selector itself finds nothing for this skill/board combo.
    target, _ = CraftCarpentry._pick_target(0.0, 4)
    assert target is None
    assert await CraftCarpentry().can_execute(ctx) is False


@pytest.mark.asyncio
async def test_fresh_carpenter_five_boards_ready():
    # Skill 0.0, 5 boards: Barrel Staves (5) is now craftable.
    ctx = _ctx(0.0, 5)
    target, _ = CraftCarpentry._pick_target(0.0, 5)
    assert target is not None
    assert await CraftCarpentry().can_execute(ctx) is True


@pytest.mark.asyncio
async def test_skilled_carpenter_four_boards_ready():
    # Skill 50, 4 boards: Barrel Lid (skill 11.0, 4 boards) is craftable, so a
    # 4-board carpenter who qualifies for it IS ready. The fast pre-filter
    # (materials >= 4) and the selector agree here.
    ctx = _ctx(50.0, 4)
    target, _ = CraftCarpentry._pick_target(50.0, 4)
    assert target is not None
    assert await CraftCarpentry().can_execute(ctx) is True


@pytest.mark.asyncio
async def test_below_min_material_still_false():
    # 3 boards is below every recipe — the cheap pre-filter rejects it.
    assert await CraftCarpentry().can_execute(_ctx(50.0, 3)) is False
    assert await CraftCarpentry().can_execute(_ctx(0.0, 3)) is False


@pytest.mark.asyncio
async def test_can_execute_matches_pick_target_across_regimes():
    # The precondition and the selector must never disagree: can_execute is
    # True iff _pick_target finds a craftable item (for any boards >= 4 that
    # pass the cheap pre-filter).
    for skill in (0.0, 11.0, 21.0, 52.6, 78.9, 100.0):
        for boards in (4, 5, 6, 8, 10, 20):
            ctx = _ctx(skill, boards)
            target, _ = CraftCarpentry._pick_target(skill, boards)
            ready = await CraftCarpentry().can_execute(ctx)
            assert ready is (target is not None), (
                f"skill={skill} boards={boards}: "
                f"can_execute={ready} but pick_target={target}"
            )
