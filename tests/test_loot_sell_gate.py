"""Backlog rank 5 (produce→gold), adventurer variant: looted weapons/armor
must flush to a vendor once a backlog piles up, so the adventurer's
worth_term stops being structurally 0.

The adventurer profession loop is first-startable-wins; bandage_self /
hunt_nearby always win, so looted gear never reaches sell_to_vendor. The
planner-level loot-flush gate counts vendor-sellable (non-KEEP) loot in the
pack and only fires once it crosses LOOT_SELL_BACKLOG_THRESHOLD.
"""
from types import SimpleNamespace

from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
from anima.skills.trade.vendor import KEEP_GRAPHICS
from anima.planner.planner import (
    LOOT_SELL_BACKLOG_THRESHOLD,
    _count_sellable_loot_in_pack,
)

# A graphic a vendor buys but that is NOT protected (real loot).
_SELLABLE = next(g for g in sorted(ITEM_VENDOR_MAP) if g not in KEEP_GRAPHICS)
# Gold is in KEEP_GRAPHICS — never counted as the sell backlog.
_KEPT = 0x0EED
_BACKPACK = 0x40000015


def _ctx(items_by_graphic):
    """items_by_graphic: {graphic: count} → ctx with that pack content."""
    items = {}
    serial = 0x100
    for graphic, count in items_by_graphic.items():
        for _ in range(count):
            items[serial] = SimpleNamespace(
                container=_BACKPACK, graphic=graphic, amount=1,
            )
            serial += 1
    ss = SimpleNamespace(equipment={0x15: _BACKPACK})
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss, world=SimpleNamespace(items=items),
        ),
        blackboard={},
    )


def test_sellable_graphic_is_real_loot():
    # Guard the test fixture: the chosen graphic must actually be sellable
    # loot, and the kept graphic must actually be protected.
    assert _SELLABLE in ITEM_VENDOR_MAP and _SELLABLE not in KEEP_GRAPHICS
    assert _KEPT in KEEP_GRAPHICS


def test_count_ignores_kept_graphics():
    # Gold (KEEP) never counts as the sell backlog, even in bulk.
    assert _count_sellable_loot_in_pack(_ctx({_KEPT: 50})) == 0
    # Sellable loot counts.
    assert _count_sellable_loot_in_pack(_ctx({_SELLABLE: 3})) == 3
    # Mixed pack: only the non-KEEP sellable loot is counted.
    assert _count_sellable_loot_in_pack(
        _ctx({_SELLABLE: 4, _KEPT: 20})
    ) == 4


def test_count_no_backpack():
    ctx = _ctx({_SELLABLE: 5})
    ctx.perception.self_state.equipment = {}  # no backpack equipped
    assert _count_sellable_loot_in_pack(ctx) == 0


def test_threshold_boundary():
    # Below threshold: gate would NOT fire (keep hunting).
    below = _ctx({_SELLABLE: LOOT_SELL_BACKLOG_THRESHOLD - 1})
    assert _count_sellable_loot_in_pack(below) < LOOT_SELL_BACKLOG_THRESHOLD
    # At threshold: gate fires (go sell the loot).
    at = _ctx({_SELLABLE: LOOT_SELL_BACKLOG_THRESHOLD})
    assert _count_sellable_loot_in_pack(at) >= LOOT_SELL_BACKLOG_THRESHOLD
