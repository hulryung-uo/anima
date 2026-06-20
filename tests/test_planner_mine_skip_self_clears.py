"""mine_ore's repeat-failure skip must be the self-clearing cooldown, never the
permanent _skip_procedures latch.

Asymmetric-latch regression: _check_stuck()'s repeat-failure handler used to do
BOTH of the following when mine_ore failed _REPEAT_FAIL_LIMIT times in a row:

  1. ctx.blackboard["_mine_exhausted_until"] = now + 300   (time-bounded, self-clearing)
  2. ctx.blackboard["_skip_procedures"].add("mine_ore")    (permanent latch)

Only mechanism (1) ever clears on its own. The _skip_procedures set is cleared
ONLY by the deadlock resolver, which fires solely when the planner goes fully
IDLE (returns None for _IDLE_ESCALATE consecutive ticks). A miner that depletes
its veins but stays busy with other work (smelt / craft / sell) never goes idle,
so the resolver never runs — and mine_ore stays latched out of _get_proc()
FOREVER, even after the 5-minute cooldown expires and the server has regenerated
the veins. The bounded cooldown was rendered moot by the unbounded latch.

The fix: the cooldown OWNS mining exclusively; mine_ore is no longer added to
the permanent set. These tests lock that in, and confirm a non-mining procedure
still uses the permanent set as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _make_planner() -> Planner:
    return Planner(ProcedureRegistry())


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=SimpleNamespace(x=10, y=20, gold=0)),
        blackboard={},
        bus=None,
    )


@pytest.mark.asyncio
async def test_mine_repeat_failure_uses_bounded_cooldown_not_permanent_latch():
    """A mine_ore failure streak sets the bounded cooldown but does NOT add
    mine_ore to the permanent _skip_procedures set (which would never clear
    while the agent keeps busy and never goes idle)."""
    import time as _time

    planner = _make_planner()
    ctx = _ctx()

    # The repeat-failure scan lives after the idle-detection early-return, so
    # the planner must be at/above the idle warn rung for it to run at all.
    planner._idle_ticks = planner._IDLE_WARN
    planner._repeat_counter["mine_ore"] = planner._REPEAT_FAIL_LIMIT
    await planner._check_stuck(ctx)

    # The self-clearing, time-bounded gate IS set...
    exhausted_until = ctx.blackboard.get("_mine_exhausted_until", 0)
    assert exhausted_until > _time.time()

    # ...but the permanent latch is NOT — otherwise mining is disabled forever
    # for any miner that stays busy and never trips the deadlock resolver.
    skip = ctx.blackboard.get("_skip_procedures", set())
    assert "mine_ore" not in skip

    # The streak counter is reset either way.
    assert planner._repeat_counter["mine_ore"] == 0


@pytest.mark.asyncio
async def test_mine_selectable_again_after_cooldown_expires():
    """Once the bounded cooldown elapses, nothing keeps mine_ore latched out:
    it is absent from _skip_procedures so the planner can re-select it as soon
    as _mine_exhausted_until is in the past (veins regenerated)."""
    import time as _time

    planner = _make_planner()
    ctx = _ctx()

    planner._idle_ticks = planner._IDLE_WARN
    planner._repeat_counter["mine_ore"] = planner._REPEAT_FAIL_LIMIT
    await planner._check_stuck(ctx)

    # Simulate the 5-minute regen window having elapsed.
    ctx.blackboard["_mine_exhausted_until"] = _time.time() - 1.0

    # No permanent latch survives the cooldown, so the exhaustion guard alone
    # decides — and it now reports "not exhausted".
    assert "mine_ore" not in ctx.blackboard.get("_skip_procedures", set())
    assert _time.time() >= ctx.blackboard["_mine_exhausted_until"]


@pytest.mark.asyncio
async def test_non_mining_procedure_still_uses_permanent_skip():
    """A non-mining procedure that fails its streak still goes into the
    permanent _skip_procedures set (cleared later by the deadlock resolver) —
    only mine_ore is special-cased to the bounded cooldown."""
    planner = _make_planner()
    ctx = _ctx()

    planner._idle_ticks = planner._IDLE_WARN
    planner._repeat_counter["smelt_ore"] = planner._REPEAT_FAIL_LIMIT
    await planner._check_stuck(ctx)

    skip = ctx.blackboard.get("_skip_procedures", set())
    assert "smelt_ore" in skip
    # A non-mining procedure does not arm the mining cooldown.
    assert "_mine_exhausted_until" not in ctx.blackboard
    assert planner._repeat_counter["smelt_ore"] == 0
