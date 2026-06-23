"""Reaching the forge must reset the walk-to-forge fail counter.

The bug: ``_craft_bs_location_fails`` is incremented on every walk-to-forge
miss and reset only when (a) it trips the 120s ``_craft_bs_location_cooldown``
at >=3, or (b) a full craft batch lands an item. It is NOT reset on the path
where its own trigger has actually cleared — the agent successfully reached a
usable forge/anvil. So a couple of transient pathfinding misses park the
counter at 1-2; the agent then reaches the forge but the batch fails for an
unrelated reason (skill-check miss / bad gump) and produces no item, so the
success reset never runs; the NEXT single walk miss trips the location cooldown
after ONE failure instead of three, needlessly locking craft_blacksmith out of
the planner for 120s. A latch cleared on the wrong path.
"""

from types import SimpleNamespace

import pytest

import anima.action.movement as movement
import anima.procedures.craft_blacksmith as cb
from anima.procedures.craft_blacksmith import CraftBlacksmith


def _make_ctx():
    ss = SimpleNamespace(
        equipment={0x15: 0x40000015},
        skills={},
        gumps={},
        x=100,
        y=100,
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items={}),
            social=SimpleNamespace(journal=[], recent=lambda count=8: []),
        ),
        conn=SimpleNamespace(connected=True),
        bus=None,
        blackboard={},
    )


@pytest.mark.asyncio
async def test_reaching_forge_resets_location_fail_counter(monkeypatch):
    proc = CraftBlacksmith()

    monkeypatch.setattr(cb, "find_in_backpack", lambda ctx, g: [object()])  # tongs

    async def _noop_close(self, ctx):
        return None
    monkeypatch.setattr(CraftBlacksmith, "_close_all_gumps", _noop_close)

    # NOT within range at the entry gate (line ~461) -> enter the walk branch;
    # then within range AFTER the walk (line ~467) -> the "arrived" path runs.
    calls = {"n": 0}

    def _anvil_forge(ctx):
        calls["n"] += 1
        return calls["n"] >= 2  # False on the gate, True after the walk
    monkeypatch.setattr(cb, "_has_anvil_and_forge", _anvil_forge)
    monkeypatch.setattr(cb, "_find_craft_walk_target", lambda ctx: (110, 110))

    async def _go_to(ctx, x, y, *args, **kwargs):
        return True  # the walk succeeds — the agent reaches the forge
    monkeypatch.setattr(movement, "go_to", _go_to)

    # Bail out cleanly right after the walk so the test isolates the reset:
    # no recipe -> execute returns before any craft attempt (and well before
    # the success path that would ALSO reset the counter).
    monkeypatch.setattr(CraftBlacksmith, "_pick_recipe", lambda self, ctx: None)

    ctx = _make_ctx()
    # Two prior transient walk misses are parked on the counter (the state a
    # couple of failed pathfinds would leave behind) — one short of the
    # >=3 trip that arms the 120s cooldown.
    ctx.blackboard["_craft_bs_location_fails"] = 2

    result = await proc.execute(ctx)

    # Execute bailed on "no recipe" (we reached the forge, then found no recipe).
    assert result.success is False
    assert "recipe" in result.message

    # The walk-to-forge trigger cleared (agent is AT the forge), so the fail
    # counter is reset — a later single walk miss must NOT immediately trip the
    # >=3 location cooldown.
    assert ctx.blackboard.get("_craft_bs_location_fails") == 0
    assert ctx.blackboard.get("_craft_bs_location_cooldown", 0) == 0
