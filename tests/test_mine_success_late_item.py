"""Yield-accounting regression: a successful mining swing must be credited
even when the ServUO "You dig some ore..." success cliloc is processed
BEFORE the container-content packet that adds the ore to the backpack.

ServUO sends the success message and the item-add (0x25/0x1A) as two
separate packets whose relative order is not guaranteed. The per-swing
wait loop breaks the moment it sees the success journal line; if the ore
item has not landed in world.items yet, ore_after == ore_before and the
swing was previously mis-booked as a BLOCKED skill-check failure with zero
yield credited. The grace re-poll recovers the credit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.mine_ore import ORE_GRAPHICS
from anima.procedures.mine_ore import MineOre


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 2500
    ctx.perception.self_state.y = 550
    ctx.perception.self_state.z = 15
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_success_cliloc_before_item_update_credits_ore():
    """The success journal line arrives first; the ore item lands one poll
    later. The swing must be booked as a success crediting the ore, not as
    a skill-check failure."""
    proc = MineOre()
    ctx = _make_ctx()
    ore_graphic = next(iter(ORE_GRAPHICS))
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: pickaxe}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        clock["t"] += d
        # Success cliloc lands on the first poll — before the item exists.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "You dig some iron ore and put it in your backpack.", 0,
            )
        # The container-content packet (ore item) arrives a couple polls
        # later, simulating the out-of-order packet processing.
        if sleeps["n"] == 3:
            ctx.perception.world.items[0x300] = MagicMock(
                container=0x101, graphic=ore_graphic, amount=4,
                serial=0x300, hue=0, x=2500, y=550,
            )
        if sleeps["n"] > 60:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.mine_ore._find_mineable_tile",
        return_value=(2500, 550, 15, 220, False),
    ), patch(
        "anima.procedures.mine_ore.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.mine_ore.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert result.success, (
        "successful swing mis-booked as failure when the success cliloc "
        "beat the ore item-update packet"
    )
    assert result.details["ore_count"] == 4


@pytest.mark.asyncio
async def test_success_cliloc_with_no_item_still_fails():
    """Guard against over-crediting: if the success line is seen but no ore
    item ever lands (genuine miss / packet truly lost), the grace re-poll
    must expire and the swing still report failure with no yield."""
    proc = MineOre()
    ctx = _make_ctx()
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: pickaxe}

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
                "You loosen some rocks but fail to find any useable ore.", 0,
            )
        if sleeps["n"] > 80:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.mine_ore._find_mineable_tile",
        return_value=(2500, 550, 15, 220, False),
    ), patch(
        "anima.procedures.mine_ore.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.mine_ore.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert not result.success
