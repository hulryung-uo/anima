"""item_name() must resolve UO tiledata %plural/singular% format codes.

Regression guard for the bug where ``name.replace("%s%", "").replace("%", "")``
left the stranded singular half of a slash form in the name (e.g.
``"rub%ies/y%"`` -> ``"rubies/y"``). The behaviour must match ClassicUO's
StringHelper.GetPluralAdjustedString (singular default).
"""

from __future__ import annotations

import pytest

import anima.data as data
from anima.data import _plural_adjust, item_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        # bare %s% suffix — singular drops it
        ("pear%s%", "pear"),
        ("fish steak%s%", "fish steak"),
        # %s% in the middle, trailing literal kept
        ("slab%s% of bacon", "slab of bacon"),
        ("jar%s% of honey", "jar of honey"),
        # slash form: token = plural/singular, singular default = second half
        ("rub%ies/y%", "ruby"),
        ("bread loa%ves/f%", "bread loaf"),
        ("wheat shea%ves/f%", "wheat sheaf"),
        ("pil%es/e% of hides", "pile of hides"),
        # %es% suffix
        ("peach%es%", "peach"),
        # no format code passes through unchanged
        ("dagger", "dagger"),
    ],
)
def test_plural_adjust_singular(raw: str, expected: str) -> None:
    assert _plural_adjust(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pear%s%", "pears"),
        ("rub%ies/y%", "rubies"),
        ("bread loa%ves/f%", "bread loaves"),
        ("pil%es/e% of hides", "piles of hides"),
    ],
)
def test_plural_adjust_plural(raw: str, expected: str) -> None:
    assert _plural_adjust(raw, plural=True) == expected


def test_item_name_resolves_slash_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """item_name no longer leaks the stranded singular half of a slash form."""
    monkeypatch.setattr(data, "_load_tiledata", lambda: {"3862": {"name": "rub%ies/y%"}})
    assert item_name(3862) == "ruby"
    assert "/" not in item_name(3862)


def test_item_name_missing_name_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed tiledata row without a 'name' key returns '' (no KeyError)."""
    monkeypatch.setattr(data, "_load_tiledata", lambda: {"42": {"weight": 1}})
    assert item_name(42) == ""


def test_item_name_unknown_graphic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data, "_load_tiledata", lambda: {})
    assert item_name(999999) == ""
