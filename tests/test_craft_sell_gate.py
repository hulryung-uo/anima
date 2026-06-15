"""Backlog rank 5: craft_blacksmith must yield to selling once a batch of
crafted items has piled up, so worth_term stops being structurally 0.

Without the gate, can_start stays True (ingots + forge present) and the
profession loop re-enters craft forever, never reaching sell_to_vendor.
"""
from types import SimpleNamespace

import pytest

import anima.procedures.craft_blacksmith as cb
from anima.procedures.craft_blacksmith import (
    SELL_BACKLOG_THRESHOLD,
    CRAFTED_ITEM_GRAPHICS,
    CraftBlacksmith,
    _count_crafted_in_pack,
)

_CRAFTED = next(iter(CRAFTED_ITEM_GRAPHICS))
_BACKPACK = 0x40000015


def _ctx(n_crafted):
    items = {
        0x100 + i: SimpleNamespace(container=_BACKPACK, graphic=_CRAFTED, amount=1)
        for i in range(n_crafted)
    }
    ss = SimpleNamespace(equipment={0x15: _BACKPACK})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=SimpleNamespace(items=items)),
        blackboard={},
    )


def test_count_crafted_in_pack():
    assert _count_crafted_in_pack(_ctx(0)) == 0
    assert _count_crafted_in_pack(_ctx(7)) == 7


@pytest.mark.asyncio
async def test_can_start_yields_when_backlog_full(monkeypatch):
    # all other preconditions pass; only the crafted backlog varies
    monkeypatch.setattr(cb, "find_in_backpack", lambda ctx, g: [object()])  # tongs
    monkeypatch.setattr(cb, "_count_iron_ingots", lambda ctx: 50)
    monkeypatch.setattr(cb, "_has_anvil_and_forge_nearby", lambda ctx: True)
    proc = CraftBlacksmith()

    below = _ctx(SELL_BACKLOG_THRESHOLD - 1)
    assert await proc.can_start(below) is True          # keep crafting

    at = _ctx(SELL_BACKLOG_THRESHOLD)
    assert await proc.can_start(at) is False            # yield → go sell
