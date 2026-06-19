"""Throughput test: the per-swing wait loop in ChopWood must break promptly
when a chop swing resolves — via new logs in the pack or a ServUO
lumberjacking result journal line (e.g. a depleted-tree message) — instead
of stalling the full ~3s deadline.

Before the fix, ChopWood.execute slept a flat ``asyncio.sleep(3.0)`` after
every swing, so a depleted/too-far/fail swing burned the whole 3s (~15 poll
iterations' worth) of dead time even though the server had already answered.
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
async def test_depleted_cliloc_breaks_wait_loop_promptly():
    """A "not enough wood" journal line must end the swing wait within a
    couple of poll iterations, not stall the full 3s deadline."""
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

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        # ServUO emits the depletion cliloc right after the swing lands.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
        # Safety valve so a non-breaking loop still terminates the test.
        if sleeps["n"] > 50:
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

    # No logs gained → the swing reports failure, but it must do so promptly.
    assert not result.success
    assert sleeps["n"] <= 3, (
        f"chop swing wait stalled {sleeps['n']} poll iters on a depletion "
        "cliloc (should break within ~1)"
    )


@pytest.mark.asyncio
async def test_logs_in_pack_breaks_wait_loop_promptly():
    """New logs appearing in the backpack must end the wait immediately."""
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

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            # A log stack lands in the pack (graphic 0x1BDD, amount 2).
            ctx.perception.world.items[0x300] = MagicMock(
                container=0x101, graphic=0x1BDD, amount=2, serial=0x300,
            )
        if sleeps["n"] > 50:
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

    assert result.success
    assert result.details and result.details.get("logs") == 2
    assert sleeps["n"] <= 3, (
        f"chop swing wait stalled {sleeps['n']} poll iters after logs landed "
        "(should break within ~1)"
    )
