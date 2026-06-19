"""Yield-accounting regression for ChopWood (mirrors test_mine_success_late_item).

A successful chop must be credited even when the ServUO "...logs into your
backpack" success cliloc is processed BEFORE the container-content packet
that adds the logs to the backpack. ServUO sends the success message and the
item-add (0x25/0x1A) as two separate packets whose relative order is not
guaranteed. The per-swing wait loop breaks the moment it sees the success
journal line; if the log item has not landed in world.items yet,
logs_after == logs_before and the swing was previously mis-booked as a
BLOCKED skill-check failure with zero yield credited. The grace re-poll
recovers the credit.
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
async def test_success_cliloc_before_log_update_credits_logs():
    """The success journal line arrives first; the log item lands a couple
    polls later. The swing must be booked as a success crediting the logs,
    not as a skill-check failure with zero yield."""
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
        # Success cliloc lands on the first poll — before the logs exist.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "You put some logs into your backpack.", 0,
            )
        # The container-content packet (log stack) arrives a couple polls
        # later, simulating out-of-order packet processing.
        if sleeps["n"] == 3:
            ctx.perception.world.items[0x300] = MagicMock(
                container=0x101, graphic=0x1BDD, amount=6,
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
        "successful chop mis-booked as failure when the success cliloc beat "
        "the log item-update packet"
    )
    assert result.details["logs"] == 6


@pytest.mark.asyncio
async def test_success_cliloc_with_no_logs_still_fails():
    """Guard against over-crediting: if the success line is seen but no logs
    ever land (genuine miss / packet truly lost), the grace re-poll must
    expire and the swing still report failure with no yield."""
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
                "You hack at the tree but fail to produce any usable wood.", 0,
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
