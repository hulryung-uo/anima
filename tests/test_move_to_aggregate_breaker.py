"""Regression: repeated blocked move_to helpers must trip an aggregate breaker."""

from __future__ import annotations

import time
from types import SimpleNamespace

from anima.planner.helpers import _MoveToProcedure
from anima.planner.planner import Planner
from anima.procedures.base import FailureReason, ProcedureRegistry, ProcedureResult


def _ctx(x: int = 2527, y: int = 548):
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=SimpleNamespace(x=x, y=y)),
        blackboard={},
    )


def _blocked(message: str = "Could not reach target") -> ProcedureResult:
    return ProcedureResult(
        success=False,
        reason=FailureReason.BLOCKED,
        message=message,
    )


def test_aggregate_move_to_failures_escalate_deadlock_recovery() -> None:
    """Different move_to targets in one stuck area must not evade breakers.

    Live symptom: Minoc recovery alternated between Bank, Blacksmith, East Mine,
    random wander stops, and relocation waypoints. Every individual procedure
    name had its own repeat counter, so each failure looked "new" while the
    avatar stayed in the same blocked tile cluster and emitted dozens of
    go_to_too_many_denials.
    """
    planner = Planner(ProcedureRegistry())
    ctx = _ctx()

    failures = [
        _MoveToProcedure("Minoc Bank", 2503, 552),
        _MoveToProcedure("Minoc Blacksmith", 2471, 564),
        _MoveToProcedure("Minoc East Mine", 2553, 496),
    ]
    for proc in failures:
        planner._record_move_to_outcome(ctx, proc, _blocked())

    assert ctx.blackboard["_move_to_blocked_streak"] == 3
    assert planner._move_fail_until > time.time() + 100
    assert ctx.blackboard["_deadlock_recovery_level"] >= 4
    assert ctx.blackboard["_deadlock_attempt_count"] == 0


def test_aggregate_move_to_success_resets_streak() -> None:
    planner = Planner(ProcedureRegistry())
    ctx = _ctx()
    proc = _MoveToProcedure("Minoc Bank", 2503, 552)

    planner._record_move_to_outcome(ctx, proc, _blocked())
    assert ctx.blackboard["_move_to_blocked_streak"] == 1

    planner._record_move_to_outcome(
        ctx,
        proc,
        ProcedureResult(success=True, message="arrived"),
    )
    assert ctx.blackboard.get("_move_to_blocked_streak", 0) == 0
