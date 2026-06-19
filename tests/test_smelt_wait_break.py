"""Throughput test: the per-swing smelt wait must break promptly when the
smelt resolves, instead of always burning a flat 2.0s nap.

Before the fix, smelt_ore.execute() called ``await asyncio.sleep(2.0)``
unconditionally after firing the smelt, so every attempt — success or an
instant skill/type/quantity failure — paid the whole 2.0s of dead time.
Because the procedure re-suggests itself (next_suggestion="smelt_ore"),
that waste compounds across the whole mine->smelt tour. The fix polls and
breaks within a couple of 0.2s iterations once the server has answered.
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
async def test_smelt_wait_breaks_promptly_on_result_cliloc():
    """A smelt-result journal line must end the per-swing wait within a
    couple of poll iterations, not stall the full 2.0s deadline."""
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    ingot_graphic = next(iter(INGOT_GRAPHICS))
    ore = MagicMock(
        container=0x101, graphic=ore_graphic, amount=5, serial=0x300, hue=0,
    )
    ctx.perception.world.items = {0x300: ore}

    social = ctx.perception.social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        # ServUO emits the success cliloc right after the smelt resolves,
        # and the ingots land in the pack.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "You smelt the ore removing the impurities and put the "
                "metal in your backpack.",
                0,
            )
            ctx.perception.world.items[0x301] = MagicMock(
                container=0x101, graphic=ingot_graphic, amount=5,
                serial=0x301, hue=0,
            )
        # Safety valve so a regressed (never-breaking) loop still ends and
        # the assertion — not a hang — reports the failure.
        if sleeps["n"] > 50:
            clock["t"] += 100.0

    async def _use_on_object(*_a, **_k):
        return MagicMock(success=True, message="ok")

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    async def _noop(*_a, **_k):
        return None

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

    # Ingots arrived → success, but the wait must have broken promptly.
    assert result.success
    assert sleeps["n"] <= 3, (
        f"smelt wait stalled {sleeps['n']} poll iters after the result "
        "cliloc (should break within ~1; a flat 2.0s nap = 10 iters)"
    )
