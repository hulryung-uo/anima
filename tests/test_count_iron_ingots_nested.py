"""_count_iron_ingots counts iron nested in a backpack sub-container.

Regression: the helper scanned only ``item.container == backpack`` (a flat,
top-level scan), so iron ingots sitting inside a bag *inside* the backpack
read as zero. That gates both craft_blacksmith.can_start and
make_tools.can_start (MIN_INGOTS / MIN_INGOTS_FOR_TOOL), so the agent would
refuse to craft despite holding plenty of iron — mirrors the recursive-scan
fix already applied to reagents/bandages (find_in_backpack BFS).

The hue-0 filter (only true Iron counts, never colored metal) must survive
the switch to the recursive scan.
"""
from types import SimpleNamespace

from anima.procedures.craft_blacksmith import IRON_HUE, _count_iron_ingots
from anima.skills.crafting.smelt import INGOT_GRAPHICS

BACKPACK_SERIAL = 0x4001
INNER_BAG_SERIAL = 0x4002
IRON_INGOT = next(iter(INGOT_GRAPHICS))


def _item(serial: int, graphic: int, *, amount: int = 1, hue: int = 0,
          container: int = BACKPACK_SERIAL) -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial, graphic=graphic, amount=amount, hue=hue,
        container=container,
    )


def _ctx(items: list[SimpleNamespace]) -> SimpleNamespace:
    ss = SimpleNamespace(equipment={0x15: BACKPACK_SERIAL})
    world = SimpleNamespace(items={it.serial: it for it in items})
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
    )


def test_iron_in_top_level_backpack_is_counted():
    ctx = _ctx([
        _item(0x100, IRON_INGOT, amount=12, hue=IRON_HUE),
    ])
    assert _count_iron_ingots(ctx) == 12


def test_iron_nested_in_subcontainer_is_counted():
    # The bag sits in the backpack; the iron sits in the bag.
    ctx = _ctx([
        _item(INNER_BAG_SERIAL, 0x0E76, amount=1, container=BACKPACK_SERIAL),
        _item(0x100, IRON_INGOT, amount=20, hue=IRON_HUE,
              container=INNER_BAG_SERIAL),
    ])
    # Old flat scan returned 0 here; the recursive scan must find all 20.
    assert _count_iron_ingots(ctx) == 20


def test_nested_plus_top_level_sum():
    ctx = _ctx([
        _item(0x100, IRON_INGOT, amount=5, hue=IRON_HUE,
              container=BACKPACK_SERIAL),
        _item(INNER_BAG_SERIAL, 0x0E76, amount=1, container=BACKPACK_SERIAL),
        _item(0x101, IRON_INGOT, amount=7, hue=IRON_HUE,
              container=INNER_BAG_SERIAL),
    ])
    assert _count_iron_ingots(ctx) == 12


def test_colored_metal_never_counts_even_when_nested():
    ctx = _ctx([
        _item(INNER_BAG_SERIAL, 0x0E76, amount=1, container=BACKPACK_SERIAL),
        # Gold-hue ingots in the bag — must NOT be counted as iron.
        _item(0x100, IRON_INGOT, amount=50, hue=0x08A5,
              container=INNER_BAG_SERIAL),
    ])
    assert _count_iron_ingots(ctx) == 0


def test_no_backpack_returns_zero():
    ss = SimpleNamespace(equipment={})
    world = SimpleNamespace(items={})
    ctx = SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, world=world),
    )
    assert _count_iron_ingots(ctx) == 0
