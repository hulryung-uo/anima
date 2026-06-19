"""Regression: find_in_backpack must scan sub-containers recursively.

UO players routinely keep stackable goods inside an inner bag — a bag of
reagents, a pouch, an ore-laden beetle pack — that itself sits in the
backpack. The old flat scan only matched items whose ``container`` was the
backpack serial itself, so anything one level deeper read as zero. That made
count_items() undercount (reagents/bandages/ore) and stalled procedures whose
preconditions check ``bool(find_in_backpack(...))``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from anima.actions.inventory import count_items, find_in_backpack
from anima.perception.world_state import ItemInfo, WorldState

BACKPACK = 0x40000001
INNER_BAG = 0x40000002
DEEP_BAG = 0x40000003
ORE_GRAPHIC = 0x19B9
BANDAGE_GRAPHIC = 0x0E21


def _ctx(items: dict[int, ItemInfo]) -> MagicMock:
    ctx = MagicMock()
    ctx.perception.self_state.equipment = {0x15: BACKPACK}
    world = WorldState()
    world.items = items
    ctx.perception.world = world
    return ctx


def _item(serial: int, container: int, graphic: int, amount: int = 1) -> ItemInfo:
    it = ItemInfo(serial=serial)
    it.container = container
    it.graphic = graphic
    it.amount = amount
    return it


def test_finds_item_directly_in_backpack():
    ctx = _ctx({1: _item(1, BACKPACK, ORE_GRAPHIC, amount=5)})
    found = find_in_backpack(ctx, {ORE_GRAPHIC})
    assert [i.amount for i in found] == [5]


def test_finds_item_inside_sub_container():
    """An ore stack inside a bag that sits in the backpack must be found."""
    items = {
        INNER_BAG: _item(INNER_BAG, BACKPACK, 0x0E76),  # a bag in the backpack
        10: _item(10, INNER_BAG, ORE_GRAPHIC, amount=20),  # ore nested in the bag
    }
    found = find_in_backpack(ctx := _ctx(items), {ORE_GRAPHIC})
    assert [i.serial for i in found] == [10]
    assert count_items(ctx, {ORE_GRAPHIC}) == 20


def test_counts_across_direct_and_nested_containers():
    items = {
        INNER_BAG: _item(INNER_BAG, BACKPACK, 0x0E76),
        1: _item(1, BACKPACK, ORE_GRAPHIC, amount=5),  # direct
        2: _item(2, INNER_BAG, ORE_GRAPHIC, amount=12),  # nested one level
    }
    assert count_items(_ctx(items), {ORE_GRAPHIC}) == 17


def test_finds_item_in_deeply_nested_container():
    """Bag-in-a-bag-in-the-backpack still counts."""
    items = {
        INNER_BAG: _item(INNER_BAG, BACKPACK, 0x0E76),
        DEEP_BAG: _item(DEEP_BAG, INNER_BAG, 0x0E76),
        99: _item(99, DEEP_BAG, BANDAGE_GRAPHIC, amount=7),
    }
    found = find_in_backpack(_ctx(items), {BANDAGE_GRAPHIC})
    assert [i.amount for i in found] == [7]


def test_ignores_items_in_unrelated_container():
    """A stack in some other container (e.g. an NPC's pack) is not counted."""
    other_pack = 0x4000DEAD
    items = {
        1: _item(1, BACKPACK, ORE_GRAPHIC, amount=5),
        2: _item(2, other_pack, ORE_GRAPHIC, amount=100),  # not reachable
    }
    assert count_items(_ctx(items), {ORE_GRAPHIC}) == 5


def test_container_cycle_does_not_hang():
    """Malformed world with a parent<->child cycle must terminate, not loop.

    INNER_BAG sits in the backpack, DEEP_BAG sits in INNER_BAG, and INNER_BAG
    *also* lists DEEP_BAG as a parent (a corrupt cycle). The BFS must still
    terminate and find the ore stored in the reachable sub-tree.
    """
    items = {
        INNER_BAG: _item(INNER_BAG, BACKPACK, 0x0E76),
        DEEP_BAG: _item(DEEP_BAG, INNER_BAG, 0x0E76),
        5: _item(5, DEEP_BAG, ORE_GRAPHIC, amount=3),
    }
    # Corrupt edge: a fourth container in DEEP_BAG that points back up to
    # INNER_BAG, forming INNER_BAG -> DEEP_BAG -> CYCLE_BAG -> INNER_BAG.
    cycle_bag = 0x40000004
    cb = _item(cycle_bag, DEEP_BAG, 0x0E76)
    items[cycle_bag] = cb
    items[INNER_BAG].container = cycle_bag  # close the loop back to INNER_BAG
    # Even with the cycle, INNER_BAG/DEEP_BAG/cycle_bag are mutually reachable
    # once any one of them is reached. Seed reachability via a sibling item
    # that sits directly in the backpack and IS one of the cyclic bags' peers.
    items[6] = _item(6, BACKPACK, 0x0E76)  # a bag directly in the backpack
    items[7] = _item(7, 6, ORE_GRAPHIC, amount=9)  # ore in that bag

    found = find_in_backpack(_ctx(items), {ORE_GRAPHIC})
    # Must return (no infinite loop) and include the cleanly-reachable ore.
    assert isinstance(found, list)
    assert 7 in {i.serial for i in found}
