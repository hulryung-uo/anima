"""Yield-accounting regression: a successful smelt must be credited even when
the ServUO success cliloc ("...put the metal in your backpack.") is processed
BEFORE the container-content packet that adds the ingots to the backpack.

ServUO sends the success message and the item-add (0x25/0x1A) as two separate
packets whose relative order is not guaranteed. The per-swing wait loop breaks
the moment it sees the success journal line; if the ingot item has not landed
in world.items yet, ingots_after == ingots_before and the smelt was previously
mis-booked as a failure with zero yield credited — and for colored ore three
such phantom misses blacklist the hue and the agent drops the ore as junk. The
grace re-poll (mirroring MineOre / ChopWood) recovers the credit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.smelt_ore import SmeltOre
from anima.skills.crafting.smelt import INGOT_GRAPHICS, ORE_GRAPHICS


def _make_ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = 2500
    ss.y = 550
    ss.z = 15
    ss.equipment = {0x15: 0x101}  # backpack serial
    ctx.perception.world.items = {}
    ctx.perception.social = SocialState()
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_success_cliloc_before_ingot_update_credits_smelt():
    """The success journal line arrives first; the ingot item lands a couple
    polls later. The smelt must be booked as a success crediting the ingots,
    not as a failure that would blacklist a (colored) ore hue."""
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    ingot_graphic = next(iter(INGOT_GRAPHICS))
    # Colored ore (non-iron hue) so a phantom miss would feed the 3-strike
    # blacklist that drops the ore as junk — the worst-case the fix prevents.
    ore = MagicMock(
        container=0x101, graphic=ore_graphic, amount=5, serial=0x300, hue=2419,
    )
    ctx.perception.world.items = {0x300: ore}

    social = ctx.perception.social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        clock["t"] += d
        # Success cliloc lands on the first poll — before the ingots exist.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "You smelt the ore removing the impurities and put the "
                "metal in your backpack.",
                0,
            )
        # The container-content packet (ingot item) arrives a couple polls
        # later, simulating out-of-order packet processing.
        if sleeps["n"] == 3:
            ctx.perception.world.items[0x301] = MagicMock(
                container=0x101, graphic=ingot_graphic, amount=5,
                serial=0x301, hue=0,
            )
        if sleeps["n"] > 60:
            clock["t"] += 100.0

    async def _use_on_object(*_a, **_k):
        return MagicMock(success=True, message="ok")

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.smelt_ore._find_forge_dynamic",
        return_value=(2500, 550, 15, 0x400),
    ), patch(
        "anima.procedures.smelt_ore._find_forge_static", return_value=None,
    ), patch(
        "anima.procedures.smelt_ore.use_on_object", new=_use_on_object,
    ), patch(
        "anima.procedures.smelt_ore.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.smelt_ore.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert result.success, (
        "successful smelt mis-booked as failure when the success cliloc beat "
        "the ingot item-update packet"
    )
    assert result.details["ingots"] == 5
    # The colored-ore hue must NOT have been blacklisted by a phantom miss.
    assert 2419 not in ctx.blackboard.get("_unsmelable_ore_hues", set())
    assert ctx.blackboard.get("_smelt_fail_counts", {}).get(2419, 0) == 0


@pytest.mark.asyncio
async def test_success_cliloc_with_no_ingot_still_fails():
    """Guard against over-crediting: if a success line is seen but no ingot
    ever lands (genuine packet loss), the grace re-poll must expire and the
    smelt still report failure with no yield."""
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    ore = MagicMock(
        container=0x101, graphic=ore_graphic, amount=5, serial=0x300, hue=0,
    )
    ctx.perception.world.items = {0x300: ore}

    social = ctx.perception.social

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
                "You smelt the ore removing the impurities and put the "
                "metal in your backpack.",
                0,
            )
        # No ingot item is ever added — the grace-poll must expire.
        if sleeps["n"] > 80:
            clock["t"] += 100.0

    async def _use_on_object(*_a, **_k):
        return MagicMock(success=True, message="ok")

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.smelt_ore._find_forge_dynamic",
        return_value=(2500, 550, 15, 0x400),
    ), patch(
        "anima.procedures.smelt_ore._find_forge_static", return_value=None,
    ), patch(
        "anima.procedures.smelt_ore.use_on_object", new=_use_on_object,
    ), patch(
        "anima.procedures.smelt_ore.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.smelt_ore.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert not result.success
