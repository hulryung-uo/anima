"""Regression: the placeholder flee node must not starve the real heal skill.

Priority-inversion guard. The root Selector orders Survival above SkillExec.
`heal_self` (the only real survival action) is registered in the SkillExec
branch. When HP < 30% the Survival sequence's Condition passes and fires the
`_flee_action` placeholder. If that placeholder returned SUCCESS, the Selector
would short-circuit and the SkillExec branch — and therefore `heal_self` —
would never run at exactly the moment the agent most needs to heal.

These tests pin `_flee_action` to FAILURE and prove the default tree falls
through to the skill branch when HP is critically low.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from anima.brain.behavior_tree import (
    Action,
    BrainContext,
    Condition,
    Selector,
    Sequence,
    Status,
)
from anima.brain.brain import _flee_action, _has_low_hp, build_default_tree


def make_ctx(hp_percent: float) -> BrainContext:
    perception = MagicMock()
    perception.self_state.hp_percent = hp_percent
    perception.self_state.hits = int(hp_percent)
    perception.self_state.hits_max = 100
    ctx = BrainContext(
        perception=perception,
        conn=MagicMock(),
        walker=MagicMock(),
        map_reader=None,
        cfg=MagicMock(),
    )
    return ctx


@pytest.mark.asyncio
async def test_flee_placeholder_returns_failure_not_success() -> None:
    """The no-op flee placeholder must FAIL so the Selector keeps traversing."""
    ctx = make_ctx(hp_percent=10.0)
    assert await _flee_action(ctx) == Status.FAILURE


@pytest.mark.asyncio
async def test_low_hp_condition_still_gates_survival() -> None:
    """Sanity: the survival condition really does fire below 30% HP."""
    assert _has_low_hp(make_ctx(hp_percent=10.0)) is True
    assert _has_low_hp(make_ctx(hp_percent=50.0)) is False


@pytest.mark.asyncio
async def test_low_hp_does_not_short_circuit_skill_branch() -> None:
    """With real low-HP HP, the default tree must reach the SkillExec branch.

    We rebuild the same shape as build_default_tree() but stub the skill
    action with a sentinel so we can observe whether it was reached. The
    survival sequence (Condition low_hp -> Action flee) precedes it. Because
    the flee placeholder now returns FAILURE, the Selector falls through to
    the skill branch even though HP is critically low.
    """
    skill_reached = False

    async def sentinel_skill(ctx: BrainContext) -> Status:
        nonlocal skill_reached
        skill_reached = True
        return Status.SUCCESS

    tree = Selector(
        "root",
        [
            Sequence(
                "survival",
                [
                    Condition("low_hp", _has_low_hp),
                    Action("flee", _flee_action),
                ],
            ),
            Sequence(
                "skill_exec",
                [
                    Condition("has_skills", lambda ctx: True),
                    Action("skill_select", sentinel_skill),
                ],
            ),
        ],
    )

    result = await tree.tick(make_ctx(hp_percent=10.0))
    assert skill_reached is True, "low HP must not starve the skill branch"
    assert result == Status.SUCCESS


@pytest.mark.asyncio
async def test_default_tree_survival_branch_is_above_skill_branch() -> None:
    """Structural guard: survival precedes skill_exec in the root Selector.

    The inversion only matters because survival is ordered first; if someone
    reorders the tree this guard documents the assumption behind the fix.
    """
    tree = build_default_tree()
    assert isinstance(tree, Selector)
    names = [getattr(child, "name", None) for child in tree.children]
    assert "survival" in names
    assert "skill_exec" in names
    assert names.index("survival") < names.index("skill_exec")
