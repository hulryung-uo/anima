"""An ambiguous partial place name resolves to the NEAREST match, not list order.

Regression: ``find_location`` partial-matched with ``name_lower in key`` and
returned the *first* candidate in ``ALL_LOCATIONS`` order. Many shop names are
distinguished only by their city prefix ("Britain Provisioner" vs "Minoc
Provisioner"), so an abbreviated LLM ``go`` pick ("provisioner") always resolved
to the Britain entry (declared first) — teleporting the goal of a miner standing
in Minoc to a shop across the world. The fix accepts the agent's position and
picks the geographically nearest partial match; the brain's ``go`` path passes
``near=(ss.x, ss.y)``.
"""

from __future__ import annotations

from anima.world_knowledge import find_location

# Minoc clusters around (2400-2600, 400-600); Britain around (1400-1660, 1550-1770).
_MINOC = (2503, 552)   # Minoc Bank
_BRITAIN = (1425, 1690)  # West Britain Bank


def test_ambiguous_partial_resolves_to_nearest_minoc():
    """Standing in Minoc, 'provisioner' must resolve to a Minoc provisioner."""
    loc = find_location("provisioner", near=_MINOC)
    assert loc is not None
    assert loc.name.startswith("Minoc"), loc.name


def test_ambiguous_partial_resolves_to_nearest_britain():
    """Standing in Britain, the same word must resolve to a Britain provisioner."""
    loc = find_location("provisioner", near=_BRITAIN)
    assert loc is not None
    assert loc.name.startswith("Britain"), loc.name


def test_blacksmith_picks_correct_city():
    """'blacksmith' is ambiguous (Britain + Minoc) — proximity disambiguates."""
    assert find_location("blacksmith", near=_MINOC).name == "Minoc Blacksmith"
    assert find_location("blacksmith", near=_BRITAIN).name == "Britain Blacksmith"


def test_no_near_preserves_first_match_order():
    """Without a position, the legacy first-in-list match is unchanged."""
    loc = find_location("provisioner")
    assert loc is not None
    # Britain Provisioner is declared before any Minoc provisioner in ALL_LOCATIONS.
    assert loc.name == "Britain Provisioner"


def test_exact_match_unaffected_by_near():
    """An exact name always wins regardless of the proximity hint."""
    loc = find_location("West Britain Bank", near=_MINOC)
    assert loc is not None
    assert loc.name == "West Britain Bank"


def test_blank_and_unknown_still_none():
    """Blank/unknown names stay None whether or not a position is supplied."""
    assert find_location("", near=_MINOC) is None
    assert find_location("   ", near=_BRITAIN) is None
    assert find_location("Castle Nonexistent Zzz", near=_MINOC) is None


def test_nearest_is_strictly_closer():
    """The chosen candidate is the minimum-distance one, not merely same-city."""
    # 'bank' matches both West/East Britain Bank and Minoc Bank.
    # From a point hard against East Britain Bank, East must win over West.
    east = find_location("bank", near=(1655, 1606))
    assert east is not None and east.name == "East Britain Bank", east.name
