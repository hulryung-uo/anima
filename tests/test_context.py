"""Tests for AgentContext (v2 migration Step 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

from anima.brain.behavior_tree import BrainContext
from anima.core.context import AgentContext


def _make_ctx(**overrides) -> AgentContext:
    """Create a minimal AgentContext for testing."""
    defaults = {
        "perception": MagicMock(),
        "conn": MagicMock(),
        "walker": MagicMock(),
        "cfg": MagicMock(),
    }
    defaults.update(overrides)
    return AgentContext(**defaults)


class TestAgentContext:
    def test_create_minimal(self):
        ctx = _make_ctx()
        assert ctx.perception is not None
        assert ctx.map_reader is None
        assert ctx.llm is None
        assert ctx.persona is None
        assert ctx.bus is None
        assert ctx.blackboard == {}

    def test_typed_fields(self):
        persona = MagicMock()
        bus = MagicMock()
        feed = MagicMock()
        ctx = _make_ctx(persona=persona, bus=bus, feed=feed)
        assert ctx.persona is persona
        assert ctx.bus is bus
        assert ctx.feed is feed

    def test_blackboard_still_works(self):
        ctx = _make_ctx(blackboard={"key": "value"})
        assert ctx.blackboard["key"] == "value"
        ctx.blackboard["new_key"] = 42
        assert ctx.blackboard.get("new_key") == 42

    def test_blackboard_and_typed_coexist(self):
        """During migration, persona is both a typed field and in the blackboard."""
        persona = MagicMock()
        ctx = _make_ctx(
            persona=persona,
            blackboard={"persona": persona, "extra": True},
        )
        assert ctx.persona is persona
        assert ctx.blackboard["persona"] is persona
        assert ctx.blackboard["extra"] is True


class TestBrainContextAlias:
    def test_alias_is_same_class(self):
        assert BrainContext is AgentContext

    def test_create_via_alias(self):
        ctx = BrainContext(
            perception=MagicMock(),
            conn=MagicMock(),
            walker=MagicMock(),
            cfg=MagicMock(),
        )
        assert isinstance(ctx, AgentContext)
        assert isinstance(ctx, BrainContext)

    def test_alias_has_blackboard(self):
        ctx = BrainContext(
            perception=MagicMock(),
            conn=MagicMock(),
            walker=MagicMock(),
            cfg=MagicMock(),
            blackboard={"test": True},
        )
        assert ctx.blackboard["test"] is True
