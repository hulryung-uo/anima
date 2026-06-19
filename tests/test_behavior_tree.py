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
    async def test_running_resumes_without_reexecuting_prior_children(self) -> None:
        """A RUNNING child must not cause earlier children to re-fire next tick.

        Regression guard: previously Sequence.tick always restarted from the
        first child, so a side-effecting Action ahead of a long-RUNNING Action
        executed once per tick instead of once per sequence run.
        """
        prior_calls = 0
        running_ticks = 0

        async def prior_action(ctx: BrainContext) -> Status:
            nonlocal prior_calls
            prior_calls += 1
            return Status.SUCCESS

        async def long_running_action(ctx: BrainContext) -> Status:
            nonlocal running_ticks
            running_ticks += 1
            # Stay RUNNING for the first two ticks, then succeed.
            return Status.RUNNING if running_ticks < 3 else Status.SUCCESS

        node = Sequence(
            "seq",
            [
                Action("prior", prior_action),
                Action("worker", long_running_action),
            ],
        )
        ctx = make_ctx()

        assert await node.tick(ctx) == Status.RUNNING
        assert await node.tick(ctx) == Status.RUNNING
        assert await node.tick(ctx) == Status.SUCCESS

        # The side-effecting prior child fired exactly once for the whole run,
        # not once per tick.
        assert prior_calls == 1
        assert running_ticks == 3

    @pytest.mark.asyncio
    async def test_resume_index_resets_after_completion(self) -> None:
        """After a sequence finishes, the next run starts from the first child."""
        prior_calls = 0
        running_ticks = 0

        async def prior_action(ctx: BrainContext) -> Status:
            nonlocal prior_calls
            prior_calls += 1
            return Status.SUCCESS

        async def worker_action(ctx: BrainContext) -> Status:
            nonlocal running_ticks
            running_ticks += 1
            return Status.RUNNING if running_ticks == 1 else Status.SUCCESS

        node = Sequence("seq", [Action("prior", prior_action), Action("worker", worker_action)])
        ctx = make_ctx()

        assert await node.tick(ctx) == Status.RUNNING  # suspends on worker
        assert await node.tick(ctx) == Status.SUCCESS  # resumes worker, completes
        assert prior_calls == 1

        # Fresh run: index was reset, so prior fires again.
        assert await node.tick(ctx) == Status.SUCCESS
        assert prior_calls == 2

    @pytest.mark.asyncio
    async def test_failure_resets_resume_index(self) -> None:
        """A FAILURE mid-run clears the resume index (next run restarts)."""
        results = iter([Status.RUNNING, Status.FAILURE])

        async def flip(ctx: BrainContext) -> Status:
            return next(results)

        prior_calls = 0

        async def prior_action(ctx: BrainContext) -> Status:
            nonlocal prior_calls
            prior_calls += 1
            return Status.SUCCESS

        node = Sequence("seq", [Action("prior", prior_action), Action("flip", flip)])
        ctx = make_ctx()

        assert await node.tick(ctx) == Status.RUNNING
        assert await node.tick(ctx) == Status.FAILURE
        # Next run must restart from the top.
        assert node._running_index == 0

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

    @pytest.mark.asyncio
    async def test_resume_rechecks_leading_guard_and_bails_when_false(self) -> None:
        """A leading Condition guard is re-checked on RUNNING-resume.

        Regression guard: previously a RUNNING child made the sequence resume
        straight at that child, never re-evaluating the gating Condition. So a
        guarded sequence (e.g. [low_hp?, heal]) stayed pinned to its action even
        after the guard went false (HP recovered), starving the rest of the tree.
        Now the guard is re-checked; if it is false the sequence bails (FAILURE)
        and does NOT re-tick the suspended action.
        """
        hp_low = True
        heal_ticks = 0

        async def guard_action(ctx: BrainContext) -> Status:
            # Stand-in heal that would stay RUNNING forever on a timer.
            nonlocal heal_ticks
            heal_ticks += 1
            return Status.RUNNING

        node = Sequence(
            "survival",
            [
                Condition("low_hp", lambda ctx: hp_low),
                Action("heal", guard_action),
            ],
        )
        ctx = make_ctx()

        # Tick 1: guard true, heal starts and stays RUNNING.
        assert await node.tick(ctx) == Status.RUNNING
        assert heal_ticks == 1

        # HP recovers — guard is now false.
        hp_low = False

        # Tick 2: resume must re-check the guard, find it false, and bail
        # WITHOUT re-ticking the heal action.
        assert await node.tick(ctx) == Status.FAILURE
        assert heal_ticks == 1  # heal NOT re-fired
        assert node._running_index == 0  # reset for a fresh run

    @pytest.mark.asyncio
    async def test_resume_keeps_running_while_guard_holds(self) -> None:
        """If the leading guard still holds, resume proceeds at the RUNNING child
        without re-firing earlier side-effecting Actions."""
        prior_calls = 0
        running_ticks = 0

        async def prior_action(ctx: BrainContext) -> Status:
            nonlocal prior_calls
            prior_calls += 1
            return Status.SUCCESS

        async def worker(ctx: BrainContext) -> Status:
            nonlocal running_ticks
            running_ticks += 1
            return Status.RUNNING if running_ticks < 2 else Status.SUCCESS

        node = Sequence(
            "guarded",
            [
                Condition("gate", lambda ctx: True),  # always holds
                Action("prior", prior_action),
                Action("worker", worker),
            ],
        )
        ctx = make_ctx()

        assert await node.tick(ctx) == Status.RUNNING
        assert await node.tick(ctx) == Status.SUCCESS
        # Guard re-checked (harmless) but the side-effecting prior fired once.
        assert prior_calls == 1
        assert running_ticks == 2


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
