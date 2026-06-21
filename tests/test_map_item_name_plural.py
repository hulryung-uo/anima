"""MapReader._get_item_name must resolve %plural/singular% codes and tolerate
partial tiledata rows.

Regression guard: anima/data.item_name was fixed (commit 5f49c79) to route
tiledata names through ClassicUO's StringHelper.GetPluralAdjustedString, but
the parallel path in anima/map.py still used the broken
``.replace("%s%", "").replace("%", "")`` and the KeyError-prone
``entry["name"]``/``entry["flags"]``/``entry["height"]`` direct indexing.
The static name flows into StaticItem.name and render_area's LLM map context.
"""

from __future__ import annotations

import pytest

from anima.map import MapReader


def _reader_with(item_data: dict) -> MapReader:
    r = MapReader(resource_dir="/nonexistent")
    # Bypass _ensure_tiledata's disk read — inject the row table directly.
    r._item_data = item_data
    r._land_flags = {}
    return r


@pytest.mark.parametrize(
    "graphic,raw,expected",
    [
        # slash form: singular default keeps the second half, no garbage slash
        (3862, "rub%ies/y%", "ruby"),
        (4445, "pil%es/e% of hides", "pile of hides"),
        (2516, "bread loa%ves/f%", "bread loaf"),
        # bare %s% suffix dropped
        (3178, "pear%s%", "pear"),
        # %s% in the middle keeps the trailing literal
        (2424, "slab%s% of bacon", "slab of bacon"),
        # plain name passes through
        (1234, "iron ore", "iron ore"),
    ],
)
def test_map_item_name_resolves_plural_codes(
    graphic: int, raw: str, expected: str
) -> None:
    r = _reader_with({str(graphic): {"name": raw, "flags": 0, "height": 0}})
    name = r._get_item_name(graphic)
    assert name == expected
    assert "%" not in name
    assert "/" not in name


def test_map_item_name_partial_row_no_keyerror() -> None:
    """A regenerated/partial tiledata row missing 'name' returns '' (no KeyError)."""
    r = _reader_with({"42": {"flags": 64}})  # no 'name', no 'height'
    assert r._get_item_name(42) == ""


def test_map_item_flags_and_height_partial_row() -> None:
    """Missing 'flags'/'height' keys default to 0 instead of KeyError."""
    r = _reader_with({"7": {"name": "thing"}})  # no 'flags', no 'height'
    assert r._get_item_flags(7) == 0
    assert r._get_item_height(7) == 0


def test_map_item_name_unknown_graphic() -> None:
    r = _reader_with({})
    assert r._get_item_name(999999) == ""
    assert r._get_item_flags(999999) == 0
    assert r._get_item_height(999999) == 0
