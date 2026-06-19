"""Throughput test: the per-swing wait loop must break promptly when a
mining swing resolves via a ServUO result cliloc — including the common
"Someone has gotten to the metal before you." (503042) race-loss line on
a contended/regenerating ore bank.

Before the fix, that line was absent from MineOre._result_snippets, so a
race-lost swing matched none of the early-break conditions and burned the
full ~3.5s deadline (~17 poll iterations) of dead time on every occurrence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
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
async def test_race_loss_cliloc_breaks_wait_loop_promptly():
    """A "someone has gotten to the metal" journal line must end the swing
    wait within a couple of poll iterations, not stall the full deadline."""
    proc = MineOre()
    ctx = _make_ctx()
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: pickaxe}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        # ServUO emits the race-loss cliloc right after the swing lands.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "Someone has gotten to the metal before you.", 0,
            )
        # Safety valve: if the loop never breaks, force the deadline so the
        # test still terminates and the assertion (not a hang) reports it.
        if sleeps["n"] > 50:
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

    # No ore gained → the swing reports failure, but it must do so promptly.
    assert not result.success
    assert sleeps["n"] <= 3, (
        f"swing wait stalled {sleeps['n']} poll iters on a race-loss cliloc "
        "(should break within ~1)"
    )
