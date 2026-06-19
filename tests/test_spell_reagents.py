"""Reagent accounting correctness for spell preconditions.

The load-bearing subtlety: a reagent can appear more than once in a spell's
reagent tuple (Recall = 2 Black Pearl + 1 Mandrake Root). A set-based "do I
have this reagent" check is WRONG for those spells — it lets a 1-pearl
inventory pass a 2-pearl cast. These tests pin per-cast counting and the
missing-reagent diff used to gate casting.
"""

from anima.core.spells import (
    REAGENT_GRAPHICS,
    get_spell_by_name,
    has_reagents_for,
    missing_reagents,
    reagent_costs,
)

_BP = REAGENT_GRAPHICS["Black Pearl"]
_MR = REAGENT_GRAPHICS["Mandrake Root"]
_GL = REAGENT_GRAPHICS["Ginseng"]
_GS = REAGENT_GRAPHICS["Garlic"]
_SS = REAGENT_GRAPHICS["Sulfurous Ash"]


def test_reagent_costs_counts_duplicates():
    # Recall is ("BP", "BP", "MR") → 2 Black Pearl, 1 Mandrake Root.
    recall = get_spell_by_name("recall")
    assert recall is not None
    assert reagent_costs(recall) == {"Black Pearl": 2, "Mandrake Root": 1}


def test_reagent_costs_reagentless_school_is_empty():
    # Chivalry spells carry no reagents.
    close_wounds = get_spell_by_name("close wounds")
    assert close_wounds is not None
    assert reagent_costs(close_wounds) == {}
    assert missing_reagents(close_wounds, {}) == []
    assert has_reagents_for(close_wounds, {})


def test_one_black_pearl_is_not_enough_for_recall():
    """A single Black Pearl must NOT satisfy a 2-pearl spell — this is the
    exact bug a set()-based check would have."""
    recall = get_spell_by_name("recall")
    have = {_BP: 1, _MR: 5}  # 1 pearl, plenty of mandrake
    assert missing_reagents(recall, have) == ["Black Pearl"]
    assert not has_reagents_for(recall, have)

    # Two pearls clears it.
    have2 = {_BP: 2, _MR: 5}
    assert missing_reagents(recall, have2) == []
    assert has_reagents_for(recall, have2)


def test_greater_heal_missing_is_sorted_and_complete():
    # Greater Heal needs GL, GS, MR, SS (one each, all distinct).
    gh = get_spell_by_name("greater heal")
    assert gh is not None
    # Have only Ginseng → the other three are missing, sorted by name.
    have = {_GL: 3}
    assert missing_reagents(gh, have) == ["Garlic", "Mandrake Root", "Sulfurous Ash"]
    # Full kit → nothing missing.
    full = {_GL: 1, _GS: 1, _MR: 1, _SS: 1}
    assert missing_reagents(gh, full) == []
    assert has_reagents_for(gh, full)


def test_zero_quantity_stack_counts_as_missing():
    gh = get_spell_by_name("greater heal")
    have = {_GL: 1, _GS: 0, _MR: 1, _SS: 1}  # garlic stack depleted to 0
    assert missing_reagents(gh, have) == ["Garlic"]
