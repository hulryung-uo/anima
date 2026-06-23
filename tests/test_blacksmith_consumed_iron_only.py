"""CraftBlacksmith consumed-delta counts ONLY Iron (hue-0) ingots.

Regression: ``can_execute``/``ingots_available`` were fixed to count only
hue-0 Iron, but the in-craft ``ingots_before``/``ingots_after`` delta still
summed every ``INGOT_GRAPHICS`` stack regardless of hue. Iron and colored
metals share graphic IDs, so a colored stack that changed for any reason made
``consumed > 0`` and let ``_classify_blacksmith_result`` promote an
unrecognized journal result to a phantom ``success`` (+5.0, fake skill gain).
The fix routes all three counts through ``_count_iron_ingots`` so the delta
reflects only the Iron pool the craft actually uses.
"""
from types import SimpleNamespace

from anima.skills.crafting.blacksmith import (
    INGOT_GRAPHICS,
    IRON_HUE,
    _classify_blacksmith_result,
    _count_iron_ingots,
)

BACKPACK = 0x4001
IRON_INGOT = 0x1BF2


def _item(graphic, *, amount=1, hue=0, container=BACKPACK):
    return SimpleNamespace(
        graphic=graphic, amount=amount, hue=hue, container=container,
    )


def _world(items):
    return SimpleNamespace(items={i: it for i, it in enumerate(items)})


def test_count_iron_ingots_ignores_colored_hue():
    world = _world([
        _item(IRON_INGOT, amount=12, hue=IRON_HUE),    # real Iron
        _item(IRON_INGOT, amount=50, hue=0x08A5),      # Gold hue — not Iron
    ])
    assert _count_iron_ingots(world, BACKPACK) == 12


def test_count_iron_ingots_skips_other_containers():
    world = _world([
        _item(IRON_INGOT, amount=12, hue=IRON_HUE, container=BACKPACK),
        _item(IRON_INGOT, amount=99, hue=IRON_HUE, container=0xDEAD),  # bank
    ])
    assert _count_iron_ingots(world, BACKPACK) == 12


def test_colored_stack_change_is_not_a_phantom_success():
    """A colored-ingot stack shrinking with NO iron consumed must read 0.

    With the old all-hue counting the colored delta leaked into ``consumed``,
    and an unrecognized journal (``result_msg == ""``) classified as ``success``.
    With iron-only counting the consumed delta is 0, so the same unrecognized
    result correctly classifies as ``none`` (no phantom win).
    """
    before = _world([
        _item(IRON_INGOT, amount=20, hue=IRON_HUE),
        _item(IRON_INGOT, amount=30, hue=0x08A5),  # colored
    ])
    after = _world([
        _item(IRON_INGOT, amount=20, hue=IRON_HUE),  # iron untouched
        _item(IRON_INGOT, amount=10, hue=0x08A5),    # colored dropped 20
    ])
    consumed = _count_iron_ingots(before, BACKPACK) - _count_iron_ingots(
        after, BACKPACK,
    )
    assert consumed == 0
    # Unrecognized journal + no iron consumed => not a success.
    assert _classify_blacksmith_result("", consumed) == "none"
    # And the all-hue (buggy) delta WOULD have been a false positive.
    buggy_before = sum(it.amount for it in before.items.values()
                       if it.graphic in INGOT_GRAPHICS)
    buggy_after = sum(it.amount for it in after.items.values()
                      if it.graphic in INGOT_GRAPHICS)
    assert _classify_blacksmith_result("", buggy_before - buggy_after) == "success"


def test_real_iron_consumption_still_counts():
    before = _world([_item(IRON_INGOT, amount=20, hue=IRON_HUE)])
    after = _world([_item(IRON_INGOT, amount=8, hue=IRON_HUE)])
    consumed = _count_iron_ingots(before, BACKPACK) - _count_iron_ingots(
        after, BACKPACK,
    )
    assert consumed == 12
    assert _classify_blacksmith_result("", consumed) == "success"
