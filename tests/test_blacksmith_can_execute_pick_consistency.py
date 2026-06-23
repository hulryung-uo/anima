"""CraftBlacksmith.can_execute must agree with _pick_target (the craft selector).

Regression: can_execute hardcoded ``ingots < 8`` while execute() picks what to
forge via the recipe loop now extracted into ``_pick_target(skill, ingots)``.
The two disagreed for a fresh smith: the cheapest recipe a skill-0 smith
qualifies for is the Dagger (min_skill -0.4, 3 ingots), so a skill-0 smith
holding 3-7 iron ingots at a valid anvil+forge could forge a Dagger but
can_execute rejected it (3..7 < 8) — a dead band that left a forgeable smith
perpetually "not ready". can_execute now defers to _pick_target, so it is
ready iff an item is genuinely forgeable with the iron + skill on hand.
"""
from types import SimpleNamespace

import pytest

from anima.skills.crafting.blacksmith import (
    BLACKSMITH_SKILL_ID,
    INGOT_GRAPHIC,
    CraftBlacksmith,
)

_BACKPACK = 0x40000015
_HAMMER_GRAPHIC = 0x13E3
_ANVIL_GRAPHIC = 0x0FAF
_FORGE_GRAPHIC = 0x0FB1


def _ctx(skill_val: float, ingot_amount: int):
    """Build a ctx: backpack holds a smith hammer + an iron-ingot stack, and an
    anvil + forge sit on adjacent tiles so _has_anvil_and_forge passes."""
    bp_items = {
        0x100: SimpleNamespace(
            container=_BACKPACK, graphic=_HAMMER_GRAPHIC, amount=1, hue=0,
        ),
        0x101: SimpleNamespace(
            container=_BACKPACK, graphic=INGOT_GRAPHIC,
            amount=ingot_amount, hue=0,
        ),
    }
    # Station statics near the smith (world coords irrelevant — nearby_items
    # returns them regardless of the distance arg in this stub).
    anvil = SimpleNamespace(container=0, graphic=_ANVIL_GRAPHIC, amount=1, hue=0)
    forge = SimpleNamespace(container=0, graphic=_FORGE_GRAPHIC, amount=1, hue=0)

    def nearby_items(x, y, distance=2):
        return [anvil, forge]

    world = SimpleNamespace(items=bp_items, nearby_items=nearby_items)
    ss = SimpleNamespace(
        x=100, y=100,
        equipment={0x15: _BACKPACK},
        weight=100,
        weight_max=400,
        skills={BLACKSMITH_SKILL_ID: SimpleNamespace(value=skill_val, lock=0)},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        blackboard={},
        map_reader=None,
    )


@pytest.mark.asyncio
async def test_fresh_smith_three_iron_ready():
    # Skill 0.0, 3 iron: the Dagger (min_skill -0.4, 3 ingots) is forgeable.
    # Before the fix can_execute returned False (3 < 8) despite a craftable pack.
    target = CraftBlacksmith._pick_target(0.0, 3)
    assert target is not None
    assert target[0] == "Dagger"
    assert await CraftBlacksmith().can_execute(_ctx(0.0, 3)) is True


@pytest.mark.asyncio
async def test_fresh_smith_seven_iron_ready():
    # 3-7 iron all forge a Dagger for a skill-0 smith — the whole dead band.
    for amount in (3, 4, 5, 6, 7):
        target = CraftBlacksmith._pick_target(0.0, amount)
        assert target is not None and target[0] == "Dagger"
        assert await CraftBlacksmith().can_execute(_ctx(0.0, amount)) is True


@pytest.mark.asyncio
async def test_fresh_smith_two_iron_not_ready():
    # 2 iron is below every recipe (cheapest is 3) — the pre-filter rejects it.
    assert CraftBlacksmith._pick_target(0.0, 2) is None
    assert await CraftBlacksmith().can_execute(_ctx(0.0, 2)) is False


@pytest.mark.asyncio
async def test_can_execute_matches_pick_target_across_regimes():
    # The precondition and the selector must never disagree.
    for skill in (0.0, 24.3, 39.1, 75.0):
        for ingots in (2, 3, 5, 8, 12, 25):
            ctx = _ctx(skill, ingots)
            target = CraftBlacksmith._pick_target(skill, ingots)
            ready = await CraftBlacksmith().can_execute(ctx)
            assert ready is (target is not None), (
                f"skill={skill} ingots={ingots}: "
                f"can_execute={ready} but pick_target={target}"
            )


@pytest.mark.asyncio
async def test_pick_target_keeps_execute_selection():
    # _pick_target picks the highest-min_skill affordable item — identical to
    # the loop execute ran inline (last match over the ascending-sorted list).
    # A 50-skill smith with 20 iron should get the costliest affordable blade.
    target = CraftBlacksmith._pick_target(50.0, 20)
    assert target is not None
    # Katana (min_skill 44.1, 8 ingots) is the highest-skill item a 50-skill
    # smith qualifies for and can afford with 20 iron.
    assert target[0] == "Katana"
