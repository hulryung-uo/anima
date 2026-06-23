"""Soak-leak guard: ``_find_nearby_tree`` must DROP a depleted_trees entry once
its cooldown has lapsed, not merely skip the guard.

``depleted_trees`` is keyed per exhausted tree and is only ever written (the
chop procedure / skill park trees there). Before the fix the read path in
``_find_nearby_tree`` checked ``now - ts < DEPLETED_COOLDOWN`` but never deleted
a stale entry, so the dict grew for the whole lifetime of the agent process —
one entry per tree ever exhausted. ``mine.py``'s ``_is_bank_depleted`` already
self-prunes the analogous ``depleted_banks`` map; this brings parity.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from anima.skills.gathering import lumber
from anima.skills.gathering.lumber import (
    DEPLETED_COOLDOWN,
    TREE_GRAPHICS,
    _find_nearby_tree,
)

TREE_GRAPHIC = sorted(TREE_GRAPHICS)[0]


def _make_ctx(depleted: dict[tuple[int, int], float]):
    ctx = MagicMock()
    ctx.perception.self_state.x = 1000
    ctx.perception.self_state.y = 1000
    ctx.perception.world.items = {}
    ctx.map_reader = None  # force the world-item fallback path
    ctx.blackboard = {"depleted_trees": depleted}
    return ctx


def _tree_item(x: int, y: int):
    return MagicMock(container=0, graphic=TREE_GRAPHIC, x=x, y=y, z=0)


def test_expired_depleted_entry_is_pruned():
    """An entry older than DEPLETED_COOLDOWN is deleted on the next scan."""
    depleted = {(1001, 1000): 0.0}  # stamped long ago
    ctx = _make_ctx(depleted)
    ctx.perception.world.items = {0x1: _tree_item(1001, 1000)}

    # now is well past the cooldown window for the stale entry.
    with patch("time.time", return_value=DEPLETED_COOLDOWN + 100.0):
        result = _find_nearby_tree(ctx)

    # The stale entry must be gone (no unbounded growth) ...
    assert (1001, 1000) not in depleted
    # ... and the now-recovered tree must be selectable again.
    assert result is not None
    assert result[0] == 1001 and result[1] == 1000


def test_fresh_depleted_entry_is_kept_and_skips_tree():
    """An entry inside the cooldown window stays and the tree is skipped."""
    now = DEPLETED_COOLDOWN + 100.0
    depleted = {(1001, 1000): now - 1.0}  # stamped 1s ago — still on cooldown
    ctx = _make_ctx(depleted)
    ctx.perception.world.items = {0x1: _tree_item(1001, 1000)}

    with patch("time.time", return_value=now):
        result = _find_nearby_tree(ctx)

    # Still tracked (not pruned) and the depleted tree is not returned.
    assert (1001, 1000) in depleted
    assert result is None


def test_repeated_scans_do_not_grow_the_map():
    """Scanning past many short-lived depletions never leaves stale keys."""
    depleted: dict[tuple[int, int], float] = {}
    ctx = _make_ctx(depleted)

    # Each iteration: a different tree was 'parked' DEPLETED_COOLDOWN+ ago, and
    # is the only tree visible now. After the scan its expired key must be gone.
    for i in range(50):
        tx = 1001 + i
        depleted[(tx, 1000)] = 0.0
        ctx.perception.world.items = {0x1: _tree_item(tx, 1000)}
        with patch("time.time", return_value=DEPLETED_COOLDOWN + 1000.0):
            _find_nearby_tree(ctx)

    assert depleted == {}, (
        f"depleted_trees leaked stale entries across scans: {depleted}"
    )


def test_static_path_also_prunes():
    """The map-static branch prunes expired entries too (not just items)."""
    depleted = {(1001, 1000): 0.0}
    ctx = _make_ctx(depleted)

    static = MagicMock(graphic=TREE_GRAPHIC, z=0)
    tile = MagicMock(statics=[static])

    def _get_tile(x: int, y: int):
        if (x, y) == (1001, 1000):
            return tile
        return MagicMock(statics=[])

    ctx.map_reader = MagicMock()
    ctx.map_reader.get_tile.side_effect = _get_tile
    ctx.perception.world.items = {}

    with patch("time.time", return_value=DEPLETED_COOLDOWN + 100.0):
        result = _find_nearby_tree(ctx)

    assert (1001, 1000) not in depleted
    assert result is not None
    assert result[0] == 1001 and result[1] == 1000


def test_lumber_module_imports_clean():
    """Trivial guard that the patched module still imports."""
    assert hasattr(lumber, "_find_nearby_tree")
