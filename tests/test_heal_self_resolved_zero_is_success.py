"""A RESOLVED bandage that healed 0 HP is a success, not a failure.

Once HealSelf gets past the no-backpack / no-bandage / no-cursor early returns
and the server accepts the target cursor, the bandage application has RESOLVED
and the bandage is consumed. Healing 0 HP in that case is the ordinary
low-skill outcome (a "barely help" roll, or healing while already near full) —
it is NOT a failure.

Regression: HealSelf used to book a resolved 0-HP heal as
``success=False, reward=-0.5``. Because brain.py drives skill_consecutive_fails,
the 3-strike force-rethink, the action-stats buckets and the location-value map
off ``success``, a +0-HP heal trained the agent AWAY from bandaging — the exact
skill a warrior must grind. This test pins:
  * resolved heal, healed == 0  -> success=True, reward >= 0
  * no bandage in pack          -> still a failure (reward < 0)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.skills.combat.healing import BANDAGE_GRAPHIC, HealSelf


def _make_ctx(*, with_bandage: bool = True):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.serial = 0xDEADBEEF
    ss.hits = 50
    ss.hits_max = 100
    ss.hp_percent = 50.0
    ss.equipment = {0x15: 0x101}  # backpack
    ss.pending_target = None
    if with_bandage:
        bandage = MagicMock(
            container=0x101, graphic=BANDAGE_GRAPHIC, serial=0x500, amount=5,
        )
        ctx.perception.world.items = {0x500: bandage}
    else:
        ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_resolved_zero_heal_is_success():
    """Cursor accepted + bandage consumed but hits unchanged -> success, reward>=0."""
    skill = HealSelf()
    ctx = _make_ctx()
    ss = ctx.perception.self_state

    issued_cursor = 0x00C0FFEE

    async def fake_sleep(_d):
        # Server answers the double-click with its issued cursor id; HP never
        # changes (the bandage resolved but healed 0).
        if ss.pending_target is None:
            ss.pending_target = {"cursor_id": issued_cursor}
        # ss.hits stays at 50 throughout -> healed == 0.

    with patch(
        "anima.skills.combat.healing.asyncio.sleep", new=fake_sleep,
    ), patch(
        "anima.skills.combat.healing.build_target_response",
        side_effect=lambda **_kw: b"\x6c",
    ), patch(
        "anima.skills.combat.healing.build_double_click",
        side_effect=lambda s: b"\x06",
    ):
        result = await skill.execute(ctx)

    assert result.success is True, (
        "a resolved bandage that healed 0 HP must not be booked as a failure"
    )
    assert result.reward >= 0.0, (
        f"resolved 0-HP heal must carry a non-negative reward, got {result.reward}"
    )
    # The consumed bandage is still reported so inventory accounting is correct.
    assert 0x500 in result.items_consumed


@pytest.mark.asyncio
async def test_no_bandage_is_still_failure():
    """No bandage in the pack is a genuine failure: success=False, reward<0."""
    skill = HealSelf()
    ctx = _make_ctx(with_bandage=False)

    sent = {"n": 0}

    async def fake_sleep(_d):
        return None

    with patch(
        "anima.skills.combat.healing.asyncio.sleep", new=fake_sleep,
    ), patch(
        "anima.skills.combat.healing.build_target_response",
        side_effect=lambda **_kw: (sent.__setitem__("n", sent["n"] + 1), b"\x6c")[1],
    ), patch(
        "anima.skills.combat.healing.build_double_click",
        side_effect=lambda s: b"\x06",
    ):
        result = await skill.execute(ctx)

    assert result.success is False
    assert result.reward < 0.0, "a no-bandage attempt must stay a negative-reward failure"
    assert sent["n"] == 0, "must not fire a target response when there is no bandage"


@pytest.mark.asyncio
async def test_no_cursor_is_still_failure():
    """A dropped/late target cursor is a genuine (negative-reward) failure."""
    skill = HealSelf()
    ctx = _make_ctx()

    async def fake_sleep(_d):
        # Never populate pending_target -> cursor never arrives.
        return None

    with patch(
        "anima.skills.combat.healing.asyncio.sleep", new=fake_sleep,
    ), patch(
        "anima.skills.combat.healing.build_target_response",
        side_effect=lambda **_kw: b"\x6c",
    ), patch(
        "anima.skills.combat.healing.build_double_click",
        side_effect=lambda s: b"\x06",
    ):
        result = await skill.execute(ctx)

    assert result.success is False
    assert result.reward < 0.0
    assert "cursor" in result.message.lower()
