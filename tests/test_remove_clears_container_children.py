"""World.remove() must drop a container's contents, not orphan them.

Regression: removing a container serial (corpse despawn / dropped bag
destroyed / backpack on a vanishing mobile via 0x1D Delete) only popped that
single serial. Every item whose `container` pointed at it lingered forever
with a dangling parent pointer — polluting nearby_items, the 0x74 vendor
buy-list correlation (collects items by `it.container == container_serial`),
and leaking memory across a long eval run.

ClassicUO's World.RemoveItem / RemoveMobile recursively RemoveItem each child
(Game/World.cs); this mirrors that.
"""

from anima.perception.world_state import WorldState


def _seed_container_with_items(w: WorldState, container: int, *child_serials: int):
    w.get_or_create_item(container).container = 0  # on ground
    for s in child_serials:
        item = w.get_or_create_item(s)
        item.container = container


def test_remove_container_drops_its_contents():
    w = WorldState()
    corpse = 0x40000001
    _seed_container_with_items(w, corpse, 0x50000001, 0x50000002, 0x50000003)

    assert len(w.items) == 4

    w.remove(corpse)

    # Container AND every child it held are gone.
    assert corpse not in w.items
    assert 0x50000001 not in w.items
    assert 0x50000002 not in w.items
    assert 0x50000003 not in w.items
    assert w.items == {}


def test_remove_container_recurses_into_nested_containers():
    """A bag inside a corpse must take its own contents with it."""
    w = WorldState()
    corpse = 0x40000001
    bag = 0x50000001
    loose = 0x50000002
    in_bag_a = 0x60000001
    in_bag_b = 0x60000002

    # corpse -> {bag, loose}; bag -> {in_bag_a, in_bag_b}
    _seed_container_with_items(w, corpse, bag, loose)
    w.get_or_create_item(in_bag_a).container = bag
    w.get_or_create_item(in_bag_b).container = bag

    assert len(w.items) == 5

    w.remove(corpse)

    assert w.items == {}


def test_remove_does_not_touch_unrelated_items():
    w = WorldState()
    corpse = 0x40000001
    _seed_container_with_items(w, corpse, 0x50000001)

    # A sibling container and a ground item that are NOT inside `corpse`.
    other = 0x40000002
    w.get_or_create_item(other).container = 0
    w.get_or_create_item(0x50000099).container = other
    ground = w.get_or_create_item(0x70000001)
    ground.container = 0

    w.remove(corpse)

    assert corpse not in w.items
    assert 0x50000001 not in w.items
    # Unrelated entities survive.
    assert other in w.items
    assert 0x50000099 in w.items
    assert 0x70000001 in w.items


def test_remove_mobile_with_worn_items_clears_worn():
    """Removing a mobile drops items parented to it (worn equipment)."""
    w = WorldState()
    mob = 0x00001234
    w.get_or_create_mobile(mob)
    worn = w.get_or_create_item(0x50000001)
    worn.container = mob  # 0x2E Equipment sets container = parent_serial

    w.remove(mob)

    assert mob not in w.mobiles
    assert 0x50000001 not in w.items


def test_remove_missing_serial_is_safe():
    w = WorldState()
    w.get_or_create_item(0x50000001).container = 0
    # Removing something that isn't tracked is a no-op, not an error.
    w.remove(0xDEADBEEF)
    assert 0x50000001 in w.items
