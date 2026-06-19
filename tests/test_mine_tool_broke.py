"""A pickaxe that wears out mid-swing (ServUO 1044038 "You have worn out
your tool!") must be reported as MISSING_RESOURCE so the planner restocks —
NOT as a generic skill-check miss that re-suggests mine_ore against a good
ore bank, and NOT as a depleted vein that blacklists the bank.

Regression: before the fix, the worn-out line was absent from both
_mine_flags and _result_snippets, so a tool-break swing (a) stalled the
full ~3.5s deadline and (b) fell through to the "Mining failed (skill
check)" branch with next_suggestion="mine_ore", looping forever with no
tool in the pack.
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
async def test_worn_out_tool_reports_missing_resource_promptly():
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

    # Routes to restock, not a loop against the (good) ore bank.
    assert not result.success
    assert result.reason is FailureReason.MISSING_RESOURCE
    assert result.next_suggestion != "mine_ore"
    assert result.details.get("tool_broke") is True
    # The vein must NOT have been tripped as depleted.
    assert not ctx.blackboard.get("depleted_banks")
    # And it must break promptly rather than stalling the full deadline.
    assert sleeps["n"] <= 3, (
        f"swing wait stalled {sleeps['n']} poll iters on a worn-out cliloc"
    )


@pytest.mark.asyncio
async def test_worn_out_via_bus_speech():
    """When a bus is wired, the worn-out detection still fires off the bus
    speech callback (the journal reconciliation is a fallback)."""
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
                cb("avatar.speech_heard", {"text": "You have worn out your tool!"})
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
    assert result.reason is FailureReason.MISSING_RESOURCE
    assert result.details.get("tool_broke") is True
    assert sleeps["n"] <= 3
