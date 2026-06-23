"""Regression: ServUO cliloc 501990 ("You burn away the impurities but are
left with less useable metal.") is the skill-check FAILURE branch of Ore.cs —
it produces ZERO ingots. It must NOT be classified as a smelt success, so it
must NOT trigger the success-gated grace-poll that waits for an ingot item that
can never arrive.

Before the fix, "burn away the impurities" was in ``_success_snippets``, so a
501990-only outcome (the most common low-skill colored-ore miss) passed
``_journal_success_seen()`` while ``ingots_gained == 0`` and burned a full 1s
grace-poll (0.1s sleeps) per miss on top of the 2s resolve poll — pure dead
time in the tight, self-re-suggesting mine->smelt loop. The fix removes 501990
from the success snippets while keeping it in ``_result_snippets`` (it does
resolve the swing, so the main poll should still break on it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.smelt_ore import SmeltOre
from anima.skills.crafting.smelt import ORE_GRAPHICS


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
async def test_burn_impurities_fails_without_grace_poll():
    """A 501990 ("burn away the impurities") outcome with no ingot ever added
    must report failure AND must not enter the success-gated grace-poll.

    We distinguish the two poll phases by sleep duration: the resolve poll uses
    0.2s sleeps, the grace-poll uses 0.1s sleeps. With 501990 mis-classified as
    a success, the grace-poll runs ~10 extra 0.1s sleeps; with the fix it runs
    zero.
    """
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    # Colored (non-iron) ore: the hue that actually hits 501990 at low skill.
    ore = MagicMock(
        container=0x101, graphic=ore_graphic, amount=5, serial=0x300, hue=2419,
    )
    ctx.perception.world.items = {0x300: ore}

    social = ctx.perception.social

    clock = {"t": 5000.0}
    sleeps = {"n": 0, "grace": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        # The grace-poll is the ONLY place that sleeps 0.1s; the resolve poll
        # sleeps 0.2s. Count grace-poll sleeps to prove we never entered it.
        if abs(d - 0.1) < 1e-9:
            sleeps["grace"] += 1
        clock["t"] += d
        # 501990 lands on the first resolve poll — it resolves the swing but
        # produces NO ingot item, ever.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "You burn away the impurities but are left with less "
                "useable metal.",
                0,
            )
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

    # 501990 produced no ingots: this is a failure, not a success.
    assert not result.success
    # And we must NOT have burned the success-only grace-poll on a line that
    # can never produce an ingot.
    assert sleeps["grace"] == 0, (
        "501990 (zero-ingot failure) must not trigger the success-gated "
        f"grace-poll, but {sleeps['grace']} grace sleeps fired"
    )
