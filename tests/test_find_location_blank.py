"""A blank/whitespace place name must resolve to ``None`` (unknown place).

Regression: ``find_location`` partial-matches with ``name_lower in key``. For an
empty string ``"" in key`` is always True, so a blank name silently returned the
*first* location (West Britain Bank). LLM ``go`` actions frequently arrive with
the place missing/null/blank, and ``think.llm_think`` passes
``action.get("place", "")`` straight into ``find_location`` — so a malformed
``go`` would commit the agent to walking to the wrong destination instead of
being rejected as an unknown place.
"""

from __future__ import annotations

import pytest

from anima.world_knowledge import find_location


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "  \n\t "])
def test_blank_place_is_unknown(blank: str):
    # Before the fix this returned the first location (West Britain Bank).
    assert find_location(blank) is None


def test_real_place_still_resolves():
    """Sanity: a genuine name still resolves (exact + partial match intact)."""
    exact = find_location("West Britain Bank")
    assert exact is not None
    assert exact.name == "West Britain Bank"

    partial = find_location("minoc bank")
    assert partial is not None
    assert "Minoc Bank" == partial.name


def test_whitespace_padded_name_still_resolves():
    """Leading/trailing whitespace around a real name is tolerated, not rejected."""
    loc = find_location("  West Britain Bank  ")
    assert loc is not None
    assert loc.name == "West Britain Bank"


def test_unknown_nonblank_name_is_none():
    """A non-blank name that matches nothing is still None (loop unaffected)."""
    assert find_location("Castle Nonexistent Zzz") is None
