"""Throughput/correctness test: ChopWood.can_start must only fire when the
nearest tree is within actual chop range (CHOP_RANGE = 2 tiles).

The procedure swings from the agent's current footing and never walks to the
tree, yet _find_nearby_tree returns the nearest tree out to SEARCH_RADIUS = 8.
Before the fix, can_start returned True for a tree 3-8 tiles away; execute()
then swung, drew a server "too far away" reply, and parked that perfectly good
tree in depleted_trees for the full ~20-minute DEPLETED_COOLDOWN. Standing
still and firing on every distant tree thus blacklisted every reachable tree
in turn and the agent chopped nothing. The gate makes can_start defer to the
planner's walk-toward-forest path until a tree is genuinely choppable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from anima.procedures.chop_wood import CHOP_RANGE, ChopWood


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 1000
    ctx.perception.self_state.y = 1000
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_can_start_false_for_out_of_range_tree():
    """A tree beyond CHOP_RANGE (but within SEARCH_RADIUS) must not start the
    procedure — otherwise the swing draws "too far away" and blacklists it."""
    proc = ChopWood()
    ctx = _make_ctx()
    # Tree 5 tiles east: inside _find_nearby_tree's SEARCH_RADIUS (8) but
    # well beyond the 2-tile chop range.
    far_tree = (1005, 1000, 0, 0x0CCA)
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    assert max(abs(far_tree[0] - 1000), abs(far_tree[1] - 1000)) > CHOP_RANGE

    with patch(
        "anima.procedures.chop_wood.find_in_backpack",
        return_value=[hatchet],
    ), patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=far_tree,
    ):
        assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_true_for_in_range_tree():
    """A tree within CHOP_RANGE must start the procedure as before."""
    proc = ChopWood()
    ctx = _make_ctx()
    near_tree = (1002, 1000, 0, 0x0CCA)  # exactly 2 tiles → choppable
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    assert max(abs(near_tree[0] - 1000), abs(near_tree[1] - 1000)) <= CHOP_RANGE

    with patch(
        "anima.procedures.chop_wood.find_in_backpack",
        return_value=[hatchet],
    ), patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=near_tree,
    ):
        assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_can_start_false_without_tree():
    """No tree at all → still False (unchanged behaviour)."""
    proc = ChopWood()
    ctx = _make_ctx()
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    with patch(
        "anima.procedures.chop_wood.find_in_backpack",
        return_value=[hatchet],
    ), patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=None,
    ):
        assert await proc.can_start(ctx) is False
