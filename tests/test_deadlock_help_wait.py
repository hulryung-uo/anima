"""Regression: help escalation must not pause autonomous recovery for 5 minutes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from anima.planner.deadlock import DeadlockResolver


def _ctx():
    ss = SimpleNamespace(
        x=2527,
        y=548,
        gold=0,
        weight=51,
        weight_max=474,
        equipment={},
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items={}),
        ),
        persona=SimpleNamespace(name="Grimm"),
        forum_client=None,
        bus=None,
        blackboard={},
        conn=SimpleNamespace(send_packet=AsyncMock()),
    )


def _planner():
    return SimpleNamespace(
        _last_escalation=0.0,
        _idle_ticks=99,
        _failed_destinations={(1, 2): 123.0},
    )


@pytest.mark.asyncio
async def test_help_escalation_uses_short_autonomous_retry_window() -> None:
    resolver = DeadlockResolver(_planner())
    ctx = _ctx()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("anima.planner.deadlock.asyncio.sleep", new=fake_sleep):
        await resolver.escalate_to_forum(ctx)

    expected_sleeps = [
        DeadlockResolver.HELP_WAIT_CHECK_INTERVAL_S,
    ] * DeadlockResolver.HELP_WAIT_CHECKS
    assert sleeps == expected_sleeps
    assert sum(sleeps) <= 30.0
    assert ctx.blackboard["planner_intent"] == "Full reset after short help wait — retrying"
    assert ctx.blackboard["_post_forum_relocate"] is True
    assert ctx.blackboard["_forum_stranded_pos"] == (2527, 548)
