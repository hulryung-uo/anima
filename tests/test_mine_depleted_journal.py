"""A vein that reports depletion (ServUO 503040 "There is no metal here to
mine.") must trip the bank cooldown via _trip_bank and surface a
WRONG_LOCATION result — even when NO event bus is wired and the cliloc is
only visible on the speech journal.

Regression: the journal reconciliation block only recovered ``tool_broke``
from the journal, so without a bus the ``depleted`` flag stayed False. The
swing then fell through to the generic "Mining failed (skill check)" branch
with next_suggestion="mine_ore", re-mining the exhausted vein forever and
never registering the bank in ``depleted_banks``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.base import FailureReason
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
    ctx.bus = None  # bus is optional — must work off the journal alone
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_depleted_vein_trips_bank_off_journal_without_bus():
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
        # ServUO 503040 lands on the journal the moment the bank is empty.
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System", "There is no metal here to mine.", 0,
            )
        if sleeps["n"] > 50:  # safety valve so a stall fails loudly
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

    # The depleted vein must be recognised, NOT mis-booked as a skill miss.
    assert not result.success
    assert result.reason is FailureReason.WRONG_LOCATION
    assert result.details.get("depleted") is True
    # The bank must have been tripped so the planner walks to a fresh spot.
    assert ctx.blackboard.get("depleted_banks"), (
        "depleted vein on the journal (no bus) never tripped the bank cooldown"
    )
    # And it must break promptly rather than stalling the full deadline.
    assert sleeps["n"] <= 3, (
        f"swing wait stalled {sleeps['n']} poll iters on a depletion cliloc"
    )


@pytest.mark.asyncio
async def test_depleted_vein_via_bus_still_trips_bank():
    """With a bus wired, depletion still fires off the speech callback (the
    journal reconciliation is purely a no-bus fallback)."""
    proc = MineOre()
    ctx = _make_ctx()

    subs: dict[str, list] = {}

    class _Bus:
        def subscribe(self, topic, cb):
            subs.setdefault(topic, []).append(cb)
            return (topic, cb)

        def unsubscribe(self, handle):
            topic, cb = handle
            subs.get(topic, []).remove(cb)

    ctx.bus = _Bus()
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: pickaxe}
    ctx.perception.social = SocialState()

    clock = {"t": 2000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            for cb in subs.get("avatar.speech_heard", []):
                cb("avatar.speech_heard", {"text": "There is no metal here to mine."})
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

    assert not result.success
    assert result.reason is FailureReason.WRONG_LOCATION
    assert result.details.get("depleted") is True
    assert ctx.blackboard.get("depleted_banks")
    assert sleeps["n"] <= 3
