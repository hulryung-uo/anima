"""Tests for Goal, GoalStack, and goal factories."""
import time
from unittest.mock import MagicMock

import pytest

from anima.planner.goals import (
    Goal, GoalStack,
    make_collect_iron_ingots_goal,
    make_clear_inventory_goal,
    make_deposit_gold_goal,
)


class TestGoal:
    def test_no_deadline_never_expires(self):
        g = Goal(name="x", description="y")
        assert g.is_expired() is False

    def test_deadline_in_future_not_expired(self):
        g = Goal(name="x", description="y", deadline=time.time() + 100)
        assert g.is_expired() is False

    def test_deadline_in_past_expired(self):
        g = Goal(name="x", description="y", deadline=time.time() - 10)
        assert g.is_expired() is True

    def test_satisfied_returns_false_without_predicate(self):
        g = Goal(name="x", description="y")
        ctx = MagicMock()
        assert g.is_satisfied(ctx) is False

    def test_satisfied_predicate_called(self):
        g = Goal(
            name="x", description="y",
            is_satisfied_fn=lambda c: True,
        )
        ctx = MagicMock()
        assert g.is_satisfied(ctx) is True

    def test_satisfied_predicate_exception_returns_false(self):
        g = Goal(
            name="x", description="y",
            is_satisfied_fn=lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        ctx = MagicMock()
        assert g.is_satisfied(ctx) is False

    def test_progress_clamped_to_0_1(self):
        g = Goal(name="x", description="y", progress_fn=lambda c: 2.5)
        assert g.progress(MagicMock()) == 1.0
        g2 = Goal(name="x", description="y", progress_fn=lambda c: -0.3)
        assert g2.progress(MagicMock()) == 0.0


class TestGoalStack:
    def test_empty_stack(self):
        s = GoalStack()
        assert s.active is None
        assert s.depth == 0

    def test_push_makes_goal_active(self):
        s = GoalStack()
        g = Goal(name="a", description="aa")
        s.push(g)
        assert s.active is g
        assert s.depth == 1

    def test_multiple_push_top_is_active(self):
        s = GoalStack()
        a = Goal(name="a", description="")
        b = Goal(name="b", description="")
        s.push(a)
        s.push(b)
        assert s.active is b

    def test_pop_returns_and_removes(self):
        s = GoalStack()
        s.push(Goal(name="a", description=""))
        popped = s.pop()
        assert popped is not None and popped.name == "a"
        assert s.active is None

    def test_update_pops_satisfied(self):
        s = GoalStack()
        s.push(Goal(name="a", description="", is_satisfied_fn=lambda c: True))
        s.update(MagicMock())
        assert s.active is None

    def test_update_pops_expired(self):
        s = GoalStack()
        s.push(Goal(
            name="a", description="", deadline=time.time() - 5,
        ))
        s.update(MagicMock())
        assert s.active is None

    def test_update_cascades_multiple_satisfied(self):
        s = GoalStack()
        s.push(Goal(name="a", description="", is_satisfied_fn=lambda c: True))
        s.push(Goal(name="b", description="", is_satisfied_fn=lambda c: True))
        s.update(MagicMock())
        assert s.depth == 0

    def test_update_stops_at_first_unsatisfied(self):
        s = GoalStack()
        s.push(Goal(name="a", description="", is_satisfied_fn=lambda c: False))
        s.push(Goal(name="b", description="", is_satisfied_fn=lambda c: True))
        s.update(MagicMock())
        # b satisfied and popped; a still pending
        assert s.active is not None and s.active.name == "a"

    def test_is_preferred_and_forbidden(self):
        s = GoalStack()
        g = Goal(
            name="x", description="",
            preferred_procedures={"mine_ore"},
            forbidden_procedures={"sell_to_vendor"},
        )
        s.push(g)
        assert s.is_preferred("mine_ore") is True
        assert s.is_preferred("craft_blacksmith") is False
        assert s.is_forbidden("sell_to_vendor") is True

    def test_empty_stack_nothing_forbidden(self):
        s = GoalStack()
        assert s.is_preferred("mine_ore") is False
        assert s.is_forbidden("mine_ore") is False

    def test_snapshot(self):
        s = GoalStack()
        s.push(Goal(name="a", description=""))
        s.push(Goal(name="b", description=""))
        snap = s.snapshot()
        assert snap["depth"] == 2
        assert snap["active"] == "b"
        assert snap["stack"] == ["a", "b"]


class TestGoalFactories:
    def _make_ctx_with_iron(self, iron: int):
        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}
        items = {}
        if iron > 0:
            item = MagicMock()
            item.container = 0x101
            item.graphic = 0x1BF2  # ingot graphic
            item.hue = 0  # iron
            item.amount = iron
            items[1] = item
        ctx.perception.world.items = items
        return ctx

    def test_collect_iron_ingots_not_satisfied_below_target(self):
        g = make_collect_iron_ingots_goal(target=50)
        ctx = self._make_ctx_with_iron(iron=20)
        assert g.is_satisfied(ctx) is False

    def test_collect_iron_ingots_satisfied_at_target(self):
        g = make_collect_iron_ingots_goal(target=50)
        ctx = self._make_ctx_with_iron(iron=50)
        assert g.is_satisfied(ctx) is True

    def test_collect_iron_ingots_progress(self):
        g = make_collect_iron_ingots_goal(target=50)
        ctx = self._make_ctx_with_iron(iron=25)
        assert abs(g.progress(ctx) - 0.5) < 0.01

    def test_deposit_gold_satisfied_when_below_threshold(self):
        g = make_deposit_gold_goal(threshold=200)
        ctx = MagicMock()
        ctx.perception.self_state.gold = 50
        assert g.is_satisfied(ctx) is True

    def test_deposit_gold_not_satisfied_at_or_above(self):
        g = make_deposit_gold_goal(threshold=200)
        ctx = MagicMock()
        ctx.perception.self_state.gold = 250
        assert g.is_satisfied(ctx) is False
