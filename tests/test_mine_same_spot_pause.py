"""Regression: the voluntary same-spot mining cooldown must pause the bank
of the *mined tile*, not the bank the player happens to be standing in.

The agent stands still and swings a pickaxe at a target tile up to
SEARCH_RADIUS (2) tiles away. When the player tile and the mined tile
straddle an 8x8 ServUO ore-bank boundary they belong to different banks,
so keying the cooldown on the player's footing pauses the wrong bank and
leaves the actually over-mined bank selectable. This test pins the
correct (mined-tile) keying.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from anima.procedures.mine_ore import MineOre
from anima.skills.gathering.mine import (
    SAME_SPOT_MINE_LIMIT,
    _bank_key,
)

# Player footing and the mined tile fall into DIFFERENT 8x8 ore banks:
#   2502 // 8 == 312  (player)        2504 // 8 == 313  (mined tile)
# They are only 2 tiles apart (within SEARCH_RADIUS) yet on opposite
# sides of a bank boundary, which is exactly the case the old code
# mishandled.
PLAYER_X, PLAYER_Y = 2502, 550
TILE_X, TILE_Y = 2504, 550


def _make_ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = PLAYER_X
    ss.y = PLAYER_Y
    ss.z = 15
    ss.serial = 0x100
    ss.hits = 100
    ss.hits_max = 100
    ss.pending_target = None
    ss.equipment = {0x15: 0x101}  # backpack
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.blackboard = {}
    return ctx


def _ore_items(amount: int):
    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ore = MagicMock(container=0x101, graphic=0x19B9, amount=amount, serial=0x300, hue=0)
    return {0x200: pickaxe, 0x300: ore}


async def _run_one_mine(ctx, ore_before: int):
    """Drive MineOre.execute once to the ore_gained>0 branch.

    Ore goes from ore_before -> ore_before+1 after the swing resolves.
    """
    proc = MineOre()
    before_items = _ore_items(ore_before)
    after_items = _ore_items(ore_before + 1)
    after_mine = [False]

    async def _use_on_target(*args, **kwargs):
        after_mine[0] = True
        return MagicMock(success=True, message="ok")

    def _items_getter():
        return after_items if after_mine[0] else before_items

    type(ctx.perception.world).items = PropertyMock(side_effect=_items_getter)

    with patch(
        "anima.procedures.mine_ore._find_mineable_tile",
        return_value=(TILE_X, TILE_Y, 15, 220, False),
    ), patch(
        "anima.procedures.mine_ore.use_on_target",
        new=_use_on_target,
    ), patch(
        "anima.procedures.mine_ore.asyncio.sleep",
        new=AsyncMock(),
    ):
        return await proc.execute(ctx)


@pytest.mark.asyncio
async def test_same_spot_pause_keys_on_mined_tile_bank():
    """After SAME_SPOT_MINE_LIMIT mines, the paused bank is the mined
    tile's bank, NOT the player's footing bank."""
    ctx = _make_ctx()

    player_bank = _bank_key(PLAYER_X, PLAYER_Y)
    tile_bank = _bank_key(TILE_X, TILE_Y)
    # Guard the premise: the two genuinely live in different banks.
    assert player_bank != tile_bank

    result = None
    for i in range(SAME_SPOT_MINE_LIMIT):
        result = await _run_one_mine(ctx, ore_before=i)
        assert result.success is True

    voluntary_cd = ctx.blackboard.get("_voluntary_cooldown_banks", {})
    # The over-mined tile's bank must be paused...
    assert tile_bank in voluntary_cd
    # ...and the player's innocent footing bank must NOT be.
    assert player_bank not in voluntary_cd


@pytest.mark.asyncio
async def test_same_spot_counter_tracks_mined_tile():
    """The consecutive-mine counter increments on the mined tile so the
    pause actually fires after SAME_SPOT_MINE_LIMIT swings at one vein."""
    ctx = _make_ctx()

    # One fewer than the limit: no pause yet.
    for i in range(SAME_SPOT_MINE_LIMIT - 1):
        await _run_one_mine(ctx, ore_before=i)
    assert not ctx.blackboard.get("_voluntary_cooldown_banks")
    same_spot = ctx.blackboard["_mine_same_spot"]
    assert same_spot["pos"] == (TILE_X, TILE_Y)
    assert same_spot["count"] == SAME_SPOT_MINE_LIMIT - 1

    # The limit-th mine trips the pause and resets the counter.
    await _run_one_mine(ctx, ore_before=SAME_SPOT_MINE_LIMIT - 1)
    assert _bank_key(TILE_X, TILE_Y) in ctx.blackboard["_voluntary_cooldown_banks"]
    assert ctx.blackboard["_mine_same_spot"]["count"] == 0
