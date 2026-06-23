"""CraftTinker.diagnose honors the dual-graphic tinker-tool family.

Regression: ``required_items = [0x1EB8]`` listed only ONE of the two graphic
IDs a tinker tool flips between (0x1EB8 / 0x1EBC = TINKER_TOOLS_GRAPHICS). The
overridden ``can_execute`` checked the whole family correctly, but the inherited
base ``Skill.diagnose`` read ``required_items`` with exact-graphic semantics —
so an agent holding the 0x1EBC variant (plus ingots) was told it was "missing
Tinker's Tools". That false shortage is fed into ``skill_problem`` (brain.py /
think.py) and the forum-research buy path, sending the agent to buy a tool it
already owns. The fix drops the misleading single-graphic ``required_items`` and
adds a family-aware ``diagnose`` override.
"""
import asyncio
from types import SimpleNamespace

from anima.skills.crafting.tinker import (
    INGOT_GRAPHIC,
    TINKER_TOOLS_GRAPHICS,
    TINKERING_SKILL_ID,
    CraftTinker,
)

BACKPACK_SERIAL = 0x4001
TINKER_TOOL_ALT = 0x1EBC  # the OTHER tinker-tool graphic variant


def _item(graphic: int, *, amount: int = 1, container: int = BACKPACK_SERIAL):
    return SimpleNamespace(graphic=graphic, amount=amount, container=container)


def _ctx(items):
    ss = SimpleNamespace(
        equipment={0x15: BACKPACK_SERIAL},
        skills={TINKERING_SKILL_ID: SimpleNamespace(value=50.0)},
        weight=10,
        weight_max=400,
    )
    world = SimpleNamespace(items={i: it for i, it in enumerate(items)})
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss, world=world))


def _run(coro):
    return asyncio.run(coro)


def test_alt_tool_variant_is_not_reported_missing():
    """Holding the 0x1EBC variant + ingots must NOT diagnose as missing tools."""
    skill = CraftTinker()
    ctx = _ctx([
        _item(TINKER_TOOL_ALT),      # 0x1EBC — a real, owned tinker tool
        _item(INGOT_GRAPHIC, amount=20),
    ])
    # can_execute already passes (family-aware), so diagnose must say "fine".
    assert _run(skill.can_execute(ctx)) is True
    assert _run(skill.diagnose(ctx)) is None


def test_no_false_missing_tools_when_only_ingots_missing():
    """With a tool but no ingots, the shortage must point at ingots, not tools."""
    skill = CraftTinker()
    ctx = _ctx([_item(TINKER_TOOL_ALT)])  # tool present, no ingots
    assert _run(skill.can_execute(ctx)) is False
    reason = _run(skill.diagnose(ctx))
    assert reason is not None
    assert "ingot" in reason.lower()
    assert "tinker tool" not in reason.lower()


def test_missing_tools_still_reported_when_truly_absent():
    """No tinker tool of EITHER variant -> a genuine missing-tools diagnosis."""
    skill = CraftTinker()
    ctx = _ctx([_item(INGOT_GRAPHIC, amount=20)])  # ingots only, no tool
    assert _run(skill.can_execute(ctx)) is False
    reason = _run(skill.diagnose(ctx))
    assert reason is not None
    assert "tinker tool" in reason.lower()


def test_both_tool_variants_are_part_of_the_family():
    """Sanity: both known graphic IDs belong to TINKER_TOOLS_GRAPHICS."""
    assert 0x1EB8 in TINKER_TOOLS_GRAPHICS
    assert TINKER_TOOL_ALT in TINKER_TOOLS_GRAPHICS
