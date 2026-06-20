"""Carpentry must count BOTH log stack graphics (0x1BDD and 0x1BE0).

A log stack flips its graphic ID between 0x1BDD and 0x1BE0 depending on stack
size — the rest of the codebase (lumber, make_boards, banking/vendor KEEP
sets, skills.state) keys on both. Carpentry used a single LOG_GRAPHIC=0x1BDD,
so logs that render as the 0x1BE0 variant counted as zero material: a fully
loaded carpenter's can_execute gated False and the craft loop stalled.
"""
from types import SimpleNamespace

import pytest

from anima.skills.crafting.carpentry import (
    BOARD_GRAPHIC,
    CARPENTRY_SKILL_ID,
    MATERIAL_GRAPHICS,
    CraftCarpentry,
)

_BACKPACK = 0x40000015
_LOG_ALT_GRAPHIC = 0x1BE0  # the second log stack graphic


def _ctx(log_graphic: int, log_amount: int):
    """Build a ctx whose backpack holds a saw + one log stack."""
    items = {
        0x100: SimpleNamespace(
            container=_BACKPACK, graphic=0x1034, amount=1, hue=0,  # normal saw
        ),
        0x101: SimpleNamespace(
            container=_BACKPACK, graphic=log_graphic, amount=log_amount, hue=0,
        ),
    }
    ss = SimpleNamespace(
        equipment={0x15: _BACKPACK},
        weight=100,
        weight_max=400,
        skills={CARPENTRY_SKILL_ID: SimpleNamespace(value=50.0, lock=0)},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items=items),
        ),
        blackboard={},
    )


def test_material_graphics_includes_both_log_stack_graphics():
    assert 0x1BDD in MATERIAL_GRAPHICS
    assert 0x1BE0 in MATERIAL_GRAPHICS
    assert BOARD_GRAPHIC in MATERIAL_GRAPHICS


@pytest.mark.asyncio
async def test_can_execute_with_alt_log_graphic():
    # Carpenter holds 20 logs rendered as the 0x1BE0 stack graphic — plenty of
    # material. Before the fix these counted as 0 and can_execute gated False.
    ctx = _ctx(_LOG_ALT_GRAPHIC, 20)
    assert await CraftCarpentry().can_execute(ctx) is True


@pytest.mark.asyncio
async def test_can_execute_with_primary_log_graphic_unaffected():
    # The original 0x1BDD path must keep working.
    ctx = _ctx(0x1BDD, 20)
    assert await CraftCarpentry().can_execute(ctx) is True


@pytest.mark.asyncio
async def test_can_execute_below_min_material_still_false():
    # Only 3 logs (< 4) — too little material in either graphic form.
    assert await CraftCarpentry().can_execute(_ctx(_LOG_ALT_GRAPHIC, 3)) is False
    assert await CraftCarpentry().can_execute(_ctx(0x1BDD, 3)) is False
