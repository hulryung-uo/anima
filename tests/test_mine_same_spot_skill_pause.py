"""Regression: the skill-path MineOre must WRITE the SAME_SPOT_MINE_LIMIT
exploration guard on each successful swing.

``mine.py`` advertised a voluntary exploration cooldown — after
``SAME_SPOT_MINE_LIMIT`` consecutive successful mines at one (tx, ty), the
bank should be added to ``_voluntary_cooldown_banks`` so ``_is_bank_depleted``
rotates the agent onward. But the success branch never WROTE that state
(``_find_mineable_tile`` only ever READ it), so the guard was dead code and
the agent could mine one respawning pool forever.

These tests stub the mine swing so it ALWAYS succeeds (deterministic — the real
swing misses at random, which is why a prior attempt's counter never reached
the limit) and pin:
  * the same-spot counter increments by exactly 1 per SUCCESS,
  * the bank enters ``_voluntary_cooldown_banks`` exactly at the limit,
  * a spot change resets the counter to 1.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.skills.gathering.mine import (
    ORE_GRAPHICS,
    SAME_SPOT_MINE_LIMIT,
    MineOre,
    _bank_key,
)

# Two tiles in DIFFERENT 8x8 ore banks (mirrors test_mine_fail_streak_per_bank):
#   2553 // 8 == 319 (bank A)        2561 // 8 == 320 (bank B)
BANK_A = (2553, 496)
BANK_B = (2561, 496)
_ORE = next(iter(ORE_GRAPHICS))


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
    # Only the pickaxe to start; the swing stub injects ore so ore_gained > 0.
    ctx.perception.world.items = {0x200: pickaxe}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    return ctx


async def _swing_success(ctx, tile: tuple[int, int]):
    """Drive MineOre.execute once, forcing the ore_gained > 0 (success) branch.

    The mocked asyncio.sleep both supplies the target cursor and injects a
    fresh ore item into the pack the first time it runs, so ore_before (counted
    before any sleep) is 0 and ore_after (counted after the result sleep) is
    positive — deterministically reaching the success branch every call.
    """
    proc = MineOre()
    ss = ctx.perception.self_state
    world = ctx.perception.world
    state = {"served": False}

    async def _sleep(*_a, **_k):
        ss.pending_target = {"cursor_id": 1}
        if not state["served"]:
            state["served"] = True
            serial = 0x300 + len(world.items)
            world.items[serial] = MagicMock(
                container=0x101, graphic=_ORE, amount=1, serial=serial
            )

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
async def test_each_success_increments_same_spot_by_exactly_one():
    """The same-spot counter must rise by exactly 1 per successful swing."""
    ctx = _make_ctx(*BANK_A)

    res = await _swing_success(ctx, BANK_A)
    assert res.success is True
    assert ctx.blackboard["_mine_same_spot"] == {"pos": BANK_A, "count": 1}

    res = await _swing_success(ctx, BANK_A)
    assert res.success is True
    # count==2 after the 2nd mocked swing (NOT stuck at 1 — each success fires).
    assert ctx.blackboard["_mine_same_spot"] == {"pos": BANK_A, "count": 2}
    # Below the limit -> bank not yet paused.
    assert _bank_key(*BANK_A) not in ctx.blackboard.get(
        "_voluntary_cooldown_banks", {}
    )


@pytest.mark.asyncio
async def test_bank_enters_voluntary_cooldown_at_limit():
    """At SAME_SPOT_MINE_LIMIT consecutive successes the bank is paused."""
    assert SAME_SPOT_MINE_LIMIT >= 2
    ctx = _make_ctx(*BANK_A)
    bk = _bank_key(*BANK_A)

    for i in range(1, SAME_SPOT_MINE_LIMIT):
        res = await _swing_success(ctx, BANK_A)
        assert res.success is True
        assert ctx.blackboard["_mine_same_spot"]["count"] == i
        assert bk not in ctx.blackboard.get("_voluntary_cooldown_banks", {})

    # The swing that hits the limit pauses the bank and resets the counter.
    res = await _swing_success(ctx, BANK_A)
    assert res.success is True
    assert bk in ctx.blackboard["_voluntary_cooldown_banks"]
    assert ctx.blackboard["_mine_same_spot"] == {"pos": None, "count": 0}


@pytest.mark.asyncio
async def test_spot_change_resets_counter_to_one():
    """Mining a different tile restarts the same-spot streak at 1."""
    ctx = _make_ctx(*BANK_A)

    await _swing_success(ctx, BANK_A)
    await _swing_success(ctx, BANK_A)
    assert ctx.blackboard["_mine_same_spot"] == {"pos": BANK_A, "count": 2}

    # Move to a different bank/tile and swing once.
    ctx.perception.self_state.x, ctx.perception.self_state.y = BANK_B
    res = await _swing_success(ctx, BANK_B)
    assert res.success is True
    assert ctx.blackboard["_mine_same_spot"] == {"pos": BANK_B, "count": 1}
    # Neither bank reached the limit, so no voluntary cooldown yet.
    assert ctx.blackboard.get("_voluntary_cooldown_banks", {}) == {}
