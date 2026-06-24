"""Regression: deadlock wander must not chain many unreachable random targets.

Live symptom: after the movement denial cap was added, ``go_to`` correctly
returned False after repeated server denials, but ``_WanderAndScavenge.run``
ignored that result and kept picking new random targets. The avatar stayed near
the same blocked Minoc corner and emitted ``go_to_too_many_denials`` repeatedly
for target after target instead of yielding to the planner.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.planner.planner import _WanderAndScavenge
from anima.procedures.base import FailureReason


def _ctx():
    ss = SimpleNamespace(
        x=2527,
        y=548,
        z=0,
        equipment={0x15: 0x40000001},
        weight=51,
        weight_max=474,
    )
    world = SimpleNamespace(items={})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
        conn=SimpleNamespace(send_packet=AsyncMock()),
        blackboard={},
    )


@pytest.mark.asyncio
async def test_wander_aborts_after_repeated_unreachable_moves(monkeypatch) -> None:
    async def fake_go_to(_ctx, _x, _y):
        return False

    monkeypatch.setattr("anima.action.movement.go_to", fake_go_to)

    proc = _WanderAndScavenge(_ctx().perception.self_state)
    result = await proc.run(_ctx())

    assert result.success is False
    assert result.reason is FailureReason.BLOCKED
    assert "unreachable wander targets" in (result.message or "")


def test_wander_failure_budget_boundary() -> None:
    assert not _WanderAndScavenge._too_many_failed_moves(
        _WanderAndScavenge.MAX_FAILED_MOVES - 1,
    )
    assert _WanderAndScavenge._too_many_failed_moves(
        _WanderAndScavenge.MAX_FAILED_MOVES,
    )
