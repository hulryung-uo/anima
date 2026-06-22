"""Regression: the deadlock-recovery exit clears ALL per-episode state.

When the agent escapes a deadlock (regains mining tools and passes
Priority 4), ``select_procedure`` runs a reset block that pops the
per-episode deadlock scratch keys off the blackboard so the NEXT
deadlock episode starts clean.

``_deadlock_last_gold`` is the baseline for the gold-progress
anti-escalation (planner.py: ``if ss.gold > _prev_gold`` resets the
escalation attempt counter). It was set inside the deadlock block but
NOT cleared on exit — unlike its siblings (``_deadlock_attempt_count``,
``_deadlock_first_pos``, the consecutive-fail counters). A stale
high-water baseline carried from a prior episode then poisons the next
one: the agent can be genuinely earning gold *within* the new episode
yet never trip the anti-escalation (its current gold is still below the
old baseline), so a deadlock that is actually resolving escalates up the
recovery ladder anyway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import Procedure, ProcedureRegistry, ProcedureResult

PICKAXE = 0x0E86


class _StubProc(Procedure):
    def __init__(self, name: str):
        self.name = name
        self.description = f"Stub {name}"

    async def can_start(self, ctx):
        return True

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} done")


def _make_ctx(**overrides):
    ctx = MagicMock()
    ctx.perception.self_state.x = 100
    ctx.perception.self_state.y = 200
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.weight = 100
    ctx.perception.self_state.weight_max = 400
    ctx.perception.self_state.gold = 50
    ctx.perception.self_state.equipment = {0x15: 0x101}
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.conn.connected = True
    ctx.blackboard = {}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    for k, v in overrides.items():
        setattr(ctx.perception.self_state, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    ctx.perception.world.items[serial] = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial
    )


@pytest.mark.asyncio
async def test_deadlock_exit_clears_last_gold_baseline():
    """A pickaxe-armed agent passing Priority 4 wipes the per-episode
    deadlock state — including the gold-progress baseline."""
    reg = ProcedureRegistry()
    reg.register(_StubProc("mine_ore"))
    planner = Planner(reg)

    ctx = _make_ctx()
    _add_item(ctx, 1, PICKAXE)  # has tool → passes Priority 4 → reaches reset

    # Stale per-episode scratch left over from a PREVIOUS deadlock episode.
    ctx.blackboard["_deadlock_last_gold"] = 5000      # stale high-water mark
    ctx.blackboard["_deadlock_attempt_count"] = 3     # sibling — already reset today

    proc = await planner.select_procedure(ctx)

    # Sanity: we actually reached the post-Priority-4 reset block.
    assert proc is not None and proc.name == "mine_ore"
    # The bug: this key survived the exit and poisoned the next episode's
    # gold-progress anti-escalation. It must be cleared like its siblings.
    assert "_deadlock_last_gold" not in ctx.blackboard
    # Regression anchor: the reset block did run (sibling key is gone too).
    assert "_deadlock_attempt_count" not in ctx.blackboard
