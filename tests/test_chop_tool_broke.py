"""A hatchet that wears out mid-swing (ServUO 1044038 "You have worn out
your tool!") must be reported as MISSING_RESOURCE so the planner restocks —
NOT as a generic "Failed to chop wood" miss, and NOT as a depleted tree that
blacklists a perfectly good forest tile.

Regression (sibling of mine_ore's tool_broke fix, commits 234a1fa / e049b68):
before this fix the worn-out line was absent from ChopWood._RESULT_SNIPPETS,
so a tool-break swing (a) stalled the full ~3s deadline and (b) fell through
to the generic BLOCKED branch with no next_suggestion, while the tree it was
swinging at was never even parked — the agent had no hatchet to swing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.base import FailureReason
from anima.procedures.chop_wood import ChopWood


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 1000
    ctx.perception.self_state.y = 1000
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_worn_out_hatchet_reports_missing_resource_promptly():
    proc = ChopWood()
    ctx = _make_ctx()
    tree = (1001, 1000, 0, 0x0CCA)
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    def fake_monotonic():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        # ServUO sends 1044038 the moment the tool's last use is spent.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System", "You have worn out your tool!", 0,
            )
        if sleeps["n"] > 50:  # safety valve so a stall fails loudly
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree", return_value=tree,
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ), patch(
        "time.monotonic", new=fake_monotonic,
    ):
        result = await proc.execute(ctx)

    # Routes to restock, not a loop against the (good) tree.
    assert not result.success
    assert result.reason is FailureReason.MISSING_RESOURCE
    assert result.details.get("tool_broke") is True
    # A worn-out tool must NOT blacklist the (good) tree as depleted.
    assert (1001, 1000) not in ctx.blackboard.get("depleted_trees", {})
    # And it must break promptly rather than stalling the full deadline.
    assert sleeps["n"] <= 3, (
        f"swing wait stalled {sleeps['n']} poll iters on a worn-out cliloc"
    )


@pytest.mark.asyncio
async def test_depleted_tree_still_parks_after_fix():
    """Guard: the tool-broke branch must not swallow the depleted-tree case."""
    proc = ChopWood()
    ctx = _make_ctx()
    tree = (1001, 1000, 0, 0x0CCA)
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}
    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 2000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
        if sleeps["n"] > 50:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree", return_value=tree,
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert result.reason is not FailureReason.MISSING_RESOURCE
    assert (1001, 1000) in ctx.blackboard.get("depleted_trees", {})
