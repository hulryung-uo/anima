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
_SA = REAGENT_GRAPHICS["Spider's Silk"]
_BM = REAGENT_GRAPHICS["Bloodmoss"]


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


def test_mana_drain_requires_spiders_silk_not_sulfurous_ash():
    """Mana Drain (circle 4) consumes Black Pearl, Mandrake Root and Spider's
    Silk per ServUO (Scripts/Spells/Fourth/ManaDrain.cs) — NOT Sulfurous Ash.

    The earlier ("BP","MR","SS") set checked Sulfurous Ash, so an agent holding
    ash but no Spider's Silk passed the precondition gate and then hit a "More
    reagents are needed" abort the gate was meant to prevent.
    """
    md = get_spell_by_name("mana drain")
    assert md is not None
    assert reagent_costs(md) == {
        "Black Pearl": 1,
        "Mandrake Root": 1,
        "Spider's Silk": 1,
    }
    # Holding ash but no Spider's Silk must report Spider's Silk missing.
    have = {_BP: 5, _MR: 5, _SS: 5}
    assert missing_reagents(md, have) == ["Spider's Silk"]
    assert not has_reagents_for(md, have)
    # The real kit (Spider's Silk present) clears it.
    full = {_BP: 1, _MR: 1, _SA: 1}
    assert missing_reagents(md, full) == []
    assert has_reagents_for(md, full)


def test_mana_vampire_requires_bloodmoss_not_a_second_black_pearl():
    """Mana Vampire (circle 7) consumes Black Pearl, Bloodmoss, Mandrake Root
    and Spider's Silk per ServUO (Scripts/Spells/Seventh/ManaVampire.cs) — one
    each, NOT two Black Pearl + Sulfurous Ash.

    The earlier ("BP","BP","MR","SS") row asked for a 2nd Black Pearl and
    Sulfurous Ash, and "BM" was mis-mapped to "Black Pearl" with no Bloodmoss
    graphic at all — so an agent with a full real kit (incl. Bloodmoss) could
    still be told it lacked reagents, while a Bloodmoss-less pack passed.
    """
    mv = get_spell_by_name("mana vampire")
    assert mv is not None
    assert reagent_costs(mv) == {
        "Black Pearl": 1,
        "Bloodmoss": 1,
        "Mandrake Root": 1,
        "Spider's Silk": 1,
    }
    # Bloodmoss must have a real, distinct inventory graphic (ServUO 0x0F7B).
    assert REAGENT_GRAPHICS["Bloodmoss"] == 0x0F7B
    assert REAGENT_GRAPHICS["Bloodmoss"] != REAGENT_GRAPHICS["Black Pearl"]
    # Everything but Bloodmoss on hand → Bloodmoss reported missing.
    have = {_BP: 5, _MR: 5, _SA: 5}
    assert missing_reagents(mv, have) == ["Bloodmoss"]
    assert not has_reagents_for(mv, have)
    # The real kit (Bloodmoss present, single Black Pearl) clears it.
    full = {_BP: 1, _BM: 1, _MR: 1, _SA: 1}
    assert missing_reagents(mv, full) == []
    assert has_reagents_for(mv, full)


def test_heal_requires_spiders_silk_not_sulfurous_ash():
    """Heal (Magery circle 1) consumes Garlic, Ginseng and Spider's Silk per
    ServUO (Scripts/Spells/First/Heal.cs: Reagent.Garlic, Reagent.Ginseng,
    Reagent.SpidersSilk) — NOT Sulfurous Ash.

    Note the codebase's deliberate swapped map (SA=Spider's Silk,
    SS=Sulfurous Ash); the earlier ("GL","GS","SS") row resolved the third
    reagent to Sulfurous Ash, so an agent carrying ash but no Spider's Silk
    passed the precondition gate on this most-used heal and then hit a "More
    reagents are needed" abort the gate exists to prevent.
    """
    heal = get_spell_by_name("heal")
    assert heal is not None
    assert reagent_costs(heal) == {
        "Ginseng": 1,
        "Garlic": 1,
        "Spider's Silk": 1,
    }
    # Holding Sulfurous Ash but no Spider's Silk must report Spider's Silk
    # missing (this is exactly the false-pass the old SS row caused).
    have = {_GL: 5, _GS: 5, _SS: 5}
    assert missing_reagents(heal, have) == ["Spider's Silk"]
    assert not has_reagents_for(heal, have)
    # The real ServUO kit (Spider's Silk present) clears it.
    full = {_GL: 1, _GS: 1, _SA: 1}
    assert missing_reagents(heal, full) == []
    assert has_reagents_for(heal, full)
