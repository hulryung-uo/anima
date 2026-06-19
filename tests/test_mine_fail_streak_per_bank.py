"""Regression: the skill-path MineOre consecutive-failure counter must be
attributed to the *mined* ServUO ore bank, not accumulated globally.

Mining misses at random even on a fully-stocked vein. The old code kept a
single global ``_mine_consec_fail`` counter, so two unlucky misses at bank A
followed by a single miss at a *different*, freshly-arrived bank B would reach
the depletion threshold (3) and trip bank B's breaker after just one swing —
retiring a perfectly live ore pool for ~10 minutes.

This test pins per-bank attribution: a miss at a new bank restarts the streak,
and three misses must all land on the SAME bank before it is marked depleted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.skills.gathering.mine import MineOre, _bank_key

# Two tiles in DIFFERENT 8x8 ore banks:
#   2553 // 8 == 319   (bank A)        2561 // 8 == 320   (bank B)
BANK_A = (2553, 496)
BANK_B = (2561, 496)


def _make_ctx(px: int, py: int):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = px
    ss.y = py
    ss.z = 15
    ss.serial = 0x100
    ss.weight = 0
    ss.weight_max = 400
    ss.pending_target = None
    ss.equipment = {0x15: 0x101}  # backpack
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    # No ore in the pack: ore_before == ore_after == 0 -> failure branch.
    ctx.perception.world.items = {0x200: pickaxe}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    return ctx


async def _swing_once(ctx, tile: tuple[int, int]):
    """Drive MineOre.execute once to the ore_gained==0 (failure) branch.

    asyncio.sleep is mocked; on the first await it supplies the target cursor
    so execute proceeds past the cursor wait and reaches the result count.
    """
    proc = MineOre()
    ss = ctx.perception.self_state

    async def _sleep(*_a, **_k):
        ss.pending_target = {"cursor_id": 1}

    tx, ty = tile
    with patch(
        "anima.skills.gathering.mine._find_mineable_tile",
        return_value=(tx, ty, 15, 220, False),
    ), patch(
        "anima.skills.gathering.mine.asyncio.sleep",
        new=AsyncMock(side_effect=_sleep),
    ):
        return await proc.execute(ctx)


@pytest.mark.asyncio
async def test_miss_at_new_bank_does_not_inherit_prior_bank_streak():
    """Two misses at bank A + one miss at bank B must NOT deplete bank B."""
    assert _bank_key(*BANK_A) != _bank_key(*BANK_B)

    ctx = _make_ctx(*BANK_A)
    # Two unlucky misses at bank A (below the threshold of 3).
    res = await _swing_once(ctx, BANK_A)
    assert res.success is False
    res = await _swing_once(ctx, BANK_A)
    assert res.success is False
    assert ctx.blackboard["_mine_consec_fail"] == 2
    assert _bank_key(*BANK_A) not in ctx.blackboard.get("depleted_banks", {})

    # Now a single miss at a DIFFERENT, freshly-arrived bank B.
    ctx.perception.self_state.x, ctx.perception.self_state.y = BANK_B
    res = await _swing_once(ctx, BANK_B)
    assert res.success is False

    # The streak restarted on bank B: count is 1, and the live bank B is NOT
    # depleted (the old global-counter bug would have tripped it at 3).
    assert ctx.blackboard["_mine_consec_fail"] == 1
    assert _bank_key(*BANK_B) not in ctx.blackboard.get("depleted_banks", {})
    assert _bank_key(*BANK_A) not in ctx.blackboard.get("depleted_banks", {})


@pytest.mark.asyncio
async def test_three_misses_same_bank_still_deplete():
    """Three consecutive misses on ONE bank still trip its depletion."""
    ctx = _make_ctx(*BANK_A)
    breaker = MagicMock()
    ctx.blackboard["_bank_breaker"] = breaker

    for _ in range(3):
        res = await _swing_once(ctx, BANK_A)
        assert res.success is False

    bk = _bank_key(*BANK_A)
    assert bk in ctx.blackboard["depleted_banks"]
    breaker.trip.assert_called_once_with(bk)
    # Counter reset after tripping, and the bank tag cleared.
    assert ctx.blackboard["_mine_consec_fail"] == 0
    assert "_mine_consec_fail_bank" not in ctx.blackboard
