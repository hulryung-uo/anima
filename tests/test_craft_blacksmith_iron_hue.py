"""CraftBlacksmith (skill) gates on IRON ingots only, not colored metal.

Regression: the skill forces the Iron material in execute() (button 5,0), but
can_execute()/execute() counted every INGOT_GRAPHICS stack regardless of hue.
A backpack holding only colored ingots (Gold/Verite/etc., non-zero hue) then
passed the >=8 gate, the craft selected Iron, and the server replied
"insufficient metal" forever — wasted gump round-trips with zero output.
The fix counts only hue-0 (Iron) ingots.
"""
import asyncio
from types import SimpleNamespace

from anima.skills.crafting.blacksmith import (
    BLACKSMITH_SKILL_ID,
    CraftBlacksmith,
)

BACKPACK_SERIAL = 0x4001
SMITH_HAMMER = 0x13E3
IRON_INGOT = 0x1BF2


def _item(graphic: int, *, amount: int = 1, hue: int = 0,
          container: int = BACKPACK_SERIAL) -> SimpleNamespace:
    return SimpleNamespace(
        graphic=graphic, amount=amount, hue=hue, container=container,
    )


def _ctx(items: list[SimpleNamespace]) -> SimpleNamespace:
    skill = SimpleNamespace(value=50.0)
    ss = SimpleNamespace(
        equipment={0x15: BACKPACK_SERIAL},
        skills={BLACKSMITH_SKILL_ID: skill},
        weight=10,
        weight_max=400,
    )
    world = SimpleNamespace(items={i: it for i, it in enumerate(items)})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
    )


def _run(coro):
    return asyncio.run(coro)


def test_colored_only_ingots_do_not_gate_can_execute(monkeypatch):
    skill = CraftBlacksmith()
    # Pretend we are standing at a valid anvil+forge.
    monkeypatch.setattr(skill, "_has_anvil_and_forge", lambda ctx: True)

    # 50 Gold ingots (hue != 0) and a hammer — plenty of metal by count, but
    # none of it is Iron, which is what the skill will actually try to use.
    ctx = _ctx([
        _item(SMITH_HAMMER),
        _item(IRON_INGOT, amount=50, hue=0x08A5),  # Gold hue
    ])
    assert _run(skill.can_execute(ctx)) is False


def test_iron_ingots_gate_can_execute(monkeypatch):
    skill = CraftBlacksmith()
    monkeypatch.setattr(skill, "_has_anvil_and_forge", lambda ctx: True)

    ctx = _ctx([
        _item(SMITH_HAMMER),
        _item(IRON_INGOT, amount=50, hue=0),  # real Iron
    ])
    assert _run(skill.can_execute(ctx)) is True


def test_mixed_stash_counts_only_iron(monkeypatch):
    skill = CraftBlacksmith()
    monkeypatch.setattr(skill, "_has_anvil_and_forge", lambda ctx: True)

    # 7 Iron (below the 8 floor) + 100 colored. Must still be rejected: the
    # colored metal cannot be substituted for the forced Iron material.
    ctx = _ctx([
        _item(SMITH_HAMMER),
        _item(IRON_INGOT, amount=7, hue=0),
        _item(IRON_INGOT, amount=100, hue=0x08A5),
    ])
    assert _run(skill.can_execute(ctx)) is False

    # Bump Iron to 8 and it should pass.
    ctx = _ctx([
        _item(SMITH_HAMMER),
        _item(IRON_INGOT, amount=8, hue=0),
        _item(IRON_INGOT, amount=100, hue=0x08A5),
    ])
    assert _run(skill.can_execute(ctx)) is True
