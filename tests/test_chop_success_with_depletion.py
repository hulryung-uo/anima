"""Regression: a tree-stripping chop must still credit its logs.

On the swing that empties a tree, ServUO emits BOTH a success line
("You put some logs into your backpack.") AND a depletion line ("There's
not enough wood here to harvest.") within the same per-swing window, and
their relative arrival order is not guaranteed. ChopWood drove its
grace-re-poll off ``result_snippet`` — the FIRST ``_RESULT_SNIPPETS``
match in journal order — and skipped the re-poll whenever that first match
was a ``_SKIP_TREE_SNIPPETS`` (depletion / too-far) line. So when the
depletion line sorted ahead of the success line AND the log item-update
packet lost the race, the successful chop was mis-booked as a BLOCKED
"Failed to chop wood" with zero yield credited. The fix gates the
grace-poll on a standalone success line (mirroring mine_ore / smelt_ore),
so the depletion-first ordering still recovers the log credit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
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
async def test_depletion_line_first_still_credits_logs():
    """Depletion line lands BEFORE the success line; the log item arrives a
    couple polls later. The swing must still be booked a success crediting the
    logs, and the tree must still be parked as depleted."""
    proc = ChopWood()
    ctx = _make_ctx()
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        clock["t"] += d
        # The depletion line sorts FIRST in the journal — this is the swing
        # that strips the last wood off the tree, so the harvest system reports
        # exhaustion ahead of (or interleaved with) the per-swing success line.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
            social.add_speech(
                0x100, "System",
                "You put some logs into your backpack.", 0,
            )
        # The container-content packet (log stack) arrives a couple polls later
        # — the out-of-order packet the grace-poll exists to wait for.
        if sleeps["n"] == 3:
            ctx.perception.world.items[0x300] = MagicMock(
                container=0x101, graphic=0x1BDD, amount=5,
                serial=0x300, hue=0,
            )
        if sleeps["n"] > 60:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=(1001, 1000, 0, 0x0CCA),
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert result.success, (
        "tree-stripping chop mis-booked as failure when the depletion line "
        "sorted ahead of the success line and the log item-update lost the race"
    )
    assert result.details["logs"] == 5
    # The depleted tree must still be parked so the loop doesn't re-target it.
    assert (1001, 1000) in ctx.blackboard.get("depleted_trees", {})


@pytest.mark.asyncio
async def test_pure_depletion_no_success_still_fails():
    """Guard against over-crediting: a depletion line with NO success line and
    no logs ever landing must still report failure (and park the tree)."""
    proc = ChopWood()
    ctx = _make_ctx()
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 2000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        clock["t"] += d
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
        if sleeps["n"] > 80:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=(2001, 2000, 0, 0x0CCA),
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert not result.success
    assert (2001, 2000) in ctx.blackboard.get("depleted_trees", {})
