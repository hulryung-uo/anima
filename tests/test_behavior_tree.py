"""Tests for the behavior tree framework."""

from __future__ import annotations

import pytest

from anima.brain.behavior_tree import (
    Action,
    BrainContext,
    Condition,
    Selector,
    Sequence,
    Status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(**overrides) -> BrainContext:
    """Create a minimal BrainContext for testing."""
    from unittest.mock import MagicMock

    defaults = {
        "perception": MagicMock(),
        "conn": MagicMock(),
        "walker": MagicMock(),
        "map_reader": None,
        "cfg": MagicMock(),
    }
    defaults.update(overrides)
    return BrainContext(**defaults)


async def success_action(ctx: BrainContext) -> Status:
    return Status.SUCCESS


async def failure_action(ctx: BrainContext) -> Status:
    return Status.FAILURE


async def running_action(ctx: BrainContext) -> Status:
    return Status.RUNNING


# ---------------------------------------------------------------------------
# Action tests
# ---------------------------------------------------------------------------


class TestAction:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        node = Action("ok", success_action)
        assert await node.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        node = Action("fail", failure_action)
        assert await node.tick(make_ctx()) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_running(self) -> None:
        node = Action("run", running_action)
        assert await node.tick(make_ctx()) == Status.RUNNING


# ---------------------------------------------------------------------------
# Condition tests
# ---------------------------------------------------------------------------


class TestCondition:
    @pytest.mark.asyncio
    async def test_true(self) -> None:
        node = Condition("yes", lambda ctx: True)
        assert await node.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_false(self) -> None:
        node = Condition("no", lambda ctx: False)
        assert await node.tick(make_ctx()) == Status.FAILURE


# ---------------------------------------------------------------------------
# Selector tests
# ---------------------------------------------------------------------------


class TestSelector:
    @pytest.mark.asyncio
    async def test_first_success(self) -> None:
        node = Selector(
            "sel",
            [
                Action("a", success_action),
                Action("b", failure_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_fallthrough_to_second(self) -> None:
        node = Selector(
            "sel",
            [
                Action("a", failure_action),
                Action("b", success_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_all_fail(self) -> None:
        node = Selector(
            "sel",
            [
                Action("a", failure_action),
                Action("b", failure_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.FAILURE

    @pytest.mark.asyncio
    async def test_running_stops_traversal(self) -> None:
        call_count = 0

        async def counting_action(ctx: BrainContext) -> Status:
            nonlocal call_count
            call_count += 1
            return Status.SUCCESS

        node = Selector(
            "sel",
            [
                Action("a", running_action),
                Action("b", counting_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.RUNNING
        assert call_count == 0  # second child not called


# ---------------------------------------------------------------------------
# Sequence tests
# ---------------------------------------------------------------------------


class TestSequence:
    @pytest.mark.asyncio
    async def test_all_succeed(self) -> None:
        node = Sequence(
            "seq",
            [
                Action("a", success_action),
                Action("b", success_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_first_fails(self) -> None:
        call_count = 0

        async def counting_action(ctx: BrainContext) -> Status:
            nonlocal call_count
            call_count += 1
            return Status.SUCCESS

        node = Sequence(
            "seq",
            [
                Action("a", failure_action),
                Action("b", counting_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.FAILURE
        assert call_count == 0  # second child not called

    @pytest.mark.asyncio
    async def test_running_propagation(self) -> None:
        node = Sequence(
            "seq",
            [
                Action("a", success_action),
                Action("b", running_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.RUNNING

    @pytest.mark.asyncio
    async def test_condition_gates_action(self) -> None:
        """Sequence with Condition + Action: condition false → FAILURE, action not called."""
        call_count = 0

        async def counting_action(ctx: BrainContext) -> Status:
            nonlocal call_count
            call_count += 1
            return Status.SUCCESS

        node = Sequence(
            "gated",
            [
                Condition("gate", lambda ctx: False),
                Action("act", counting_action),
            ],
        )
        assert await node.tick(make_ctx()) == Status.FAILURE
        assert call_count == 0


# ---------------------------------------------------------------------------
# Composite tree tests
# ---------------------------------------------------------------------------


class TestCompositeTree:
    @pytest.mark.asyncio
    async def test_selector_with_guarded_sequences(self) -> None:
        """Mimics the default brain tree structure."""
        tree = Selector(
            "root",
            [
                Sequence(
                    "survival",
                    [
                        Condition("low_hp", lambda ctx: False),  # HP fine
                        Action("flee", success_action),
                    ],
                ),
                Sequence(
                    "social",
                    [
                        Condition("speech", lambda ctx: False),  # No speech
                        Action("respond", success_action),
                    ],
                ),
                Action("wander", success_action),
            ],
        )
        # Both sequences fail at their conditions, so wander runs
        assert await tree.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_survival_triggers(self) -> None:
        tree = Selector(
            "root",
            [
                Sequence(
                    "survival",
                    [
                        Condition("low_hp", lambda ctx: True),  # HP low!
                        Action("flee", success_action),
                    ],
                ),
                Action("wander", failure_action),  # Should not be reached
            ],
        )
        assert await tree.tick(make_ctx()) == Status.SUCCESS

    @pytest.mark.asyncio
    async def test_blackboard_integration(self) -> None:
        """Condition can read from blackboard."""
        ctx = make_ctx()
        ctx.blackboard["alert"] = True

        tree = Selector(
            "root",
            [
                Sequence(
                    "alert_handler",
                    [
                        Condition("has_alert", lambda c: c.blackboard.get("alert", False)),
                        Action("handle", success_action),
                    ],
                ),
            ],
        )
        assert await tree.tick(ctx) == Status.SUCCESS
