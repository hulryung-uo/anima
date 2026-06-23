"""Spell definitions for all UO magic schools.

Extracted from Razor CE (etc/spells.def + Core/Spells.cs).
Used by the Mage persona and combat AI for spell selection and casting.

Spell ID calculation:
  - Magery (circles 1-8): id = (circle - 1) * 8 + number
  - Other schools:        id = circle * 10 + number

Casting via packet 0x12 (TextCommand):
  build_cast_spell(spell_id) sends the cast command to the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SpellFlag(str, Enum):
    BENEFICIAL = "B"
    HARMFUL = "H"
    NEUTRAL = "N"


class SpellSchool(str, Enum):
    MAGERY = "magery"
    NECROMANCY = "necromancy"
    CHIVALRY = "chivalry"
    BUSHIDO = "bushido"
    NINJITSU = "ninjitsu"
    SPELLWEAVING = "spellweaving"
    MYSTICISM = "mysticism"
    MASTERIES = "masteries"


# Reagent abbreviations
REAGENT_NAMES: dict[str, str] = {
    "BM": "Black Pearl",
    "BP": "Black Pearl",
    "NS": "Nightshade",
    "GL": "Ginseng",
    "GS": "Garlic",
    "MR": "Mandrake Root",
    "SA": "Spider's Silk",
    "SS": "Sulfurous Ash",
    # Necromancy reagents
    "DB": "Daemon Blood",
    "GD": "Grave Dust",
    "BW": "Batwing",
    "NC": "Nox Crystal",
    "PI": "Pig Iron",
}

# Reagent item graphic IDs (for inventory checks)
REAGENT_GRAPHICS: dict[str, int] = {
    "Black Pearl": 0x0F7A,
    "Nightshade": 0x0F88,
    "Ginseng": 0x0F85,
    "Garlic": 0x0F84,
    "Mandrake Root": 0x0F86,
    "Spider's Silk": 0x0F8D,
    "Sulfurous Ash": 0x0F8C,
    "Daemon Blood": 0x0F7D,
    "Grave Dust": 0x0F81,
    "Batwing": 0x0F78,
    "Nox Crystal": 0x0F8E,
    "Pig Iron": 0x0F8A,
}


@dataclass(frozen=True)
class SpellDef:
    """Definition of a single spell."""

    id: int  # unique spell ID used in cast packet
    number: int  # spell number within its circle
    circle: int  # circle (determines school)
    name: str
    words_of_power: str  # incantation words (empty for some schools)
    flag: SpellFlag  # beneficial / harmful / neutral
    school: SpellSchool
    reagents: tuple[str, ...]  # reagent abbreviations


def _school_from_circle(circle: int) -> SpellSchool:
    if circle <= 8:
        return SpellSchool.MAGERY
    return {
        10: SpellSchool.NECROMANCY,
        20: SpellSchool.CHIVALRY,
        40: SpellSchool.BUSHIDO,
        50: SpellSchool.NINJITSU,
        60: SpellSchool.SPELLWEAVING,
        65: SpellSchool.MYSTICISM,
        70: SpellSchool.MASTERIES,
    }.get(circle, SpellSchool.MAGERY)


def _spell_id(number: int, circle: int) -> int:
    if circle < 10:
        return (circle - 1) * 8 + number
    return circle * 10 + number


def _parse_flag(s: str) -> SpellFlag:
    return {"B": SpellFlag.BENEFICIAL, "H": SpellFlag.HARMFUL}.get(
        s, SpellFlag.NEUTRAL
    )


# fmt: off
_RAW_SPELLS: list[tuple[str, int, int, str, str, tuple[str, ...]]] = [
    # flag, number, circle, name, words_of_power, reagents
    # ── Magery Circle 1 ──
    ("H", 1, 1, "Clumsy", "Uus Jux", ("BP", "NS")),
    ("N", 2, 1, "Create Food", "In Mani Ylem", ("GL", "GS", "MR")),
    ("H", 3, 1, "Feeblemind", "Rel Wis", ("GS", "NS")),
    ("B", 4, 1, "Heal", "In Mani", ("GL", "GS", "SS")),
    ("H", 5, 1, "Magic Arrow", "In Por Ylem", ("SA",)),
    ("N", 6, 1, "Night Sight", "In Lor", ("SS", "SA")),
    ("B", 7, 1, "Reactive Armor", "Flam Sanct", ("GL", "SA", "SS")),
    ("H", 8, 1, "Weaken", "Des Mani", ("GL", "NS")),
    # ── Magery Circle 2 ──
    ("B", 1, 2, "Agility", "Ex Uus", ("BP", "MR")),
    ("B", 2, 2, "Cunning", "Uus Wis", ("MR", "NS")),
    ("B", 3, 2, "Cure", "An Nox", ("GL", "GS")),
    ("H", 4, 2, "Harm", "An Mani", ("NS", "SS")),
    ("N", 5, 2, "Magic Trap", "In Jux", ("GL", "SA", "SS")),
    ("N", 6, 2, "Magic Untrap", "An Jux", ("BP", "SA")),
    ("B", 7, 2, "Protection", "Uus Sanct", ("GL", "GS", "SA")),
    ("B", 8, 2, "Strength", "Uus Mani", ("MR", "NS")),
    # ── Magery Circle 3 ──
    ("B", 1, 3, "Bless", "Rel Sanct", ("GL", "MR")),
    ("H", 2, 3, "Fireball", "Vas Flam", ("BP",)),
    ("N", 3, 3, "Magic Lock", "An Por", ("SA", "BP", "GL")),
    ("H", 4, 3, "Poison", "In Nox", ("NS",)),
    ("N", 5, 3, "Telekinesis", "Ort Por Ylem", ("BP", "MR")),
    ("N", 6, 3, "Teleport", "Rel Por", ("BP", "MR")),
    ("N", 7, 3, "Unlock", "Ex Por", ("BP", "SA")),
    ("N", 8, 3, "Wall of Stone", "In Sanct Ylem", ("BP", "GL")),
    # ── Magery Circle 4 ──
    ("B", 1, 4, "Arch Cure", "Vas An Nox", ("GL", "GS", "MR")),
    ("B", 2, 4, "Arch Protection", "Vas Uus Sanct", ("GL", "GS", "MR", "SA")),
    ("H", 3, 4, "Curse", "Des Sanct", ("GL", "NS", "SA")),
    ("N", 4, 4, "Fire Field", "In Flam Grav", ("BP", "SS", "SA")),
    ("B", 5, 4, "Greater Heal", "In Vas Mani", ("GL", "GS", "MR", "SS")),
    ("H", 6, 4, "Lightning", "Por Ort Grav", ("MR", "SA")),
    ("H", 7, 4, "Mana Drain", "Ort Rel", ("BP", "MR", "SA")),
    ("N", 8, 4, "Recall", "Kal Ort Por", ("BP", "BP", "MR")),
    # ── Magery Circle 5 ──
    ("H", 1, 5, "Blade Spirits", "In Jux Hur Ylem", ("BP", "MR", "NS")),
    ("N", 2, 5, "Dispel Field", "An Grav", ("GL", "BP", "SS", "SA")),
    ("N", 3, 5, "Incognito", "Kal In Ex", ("BP", "GL", "NS")),
    ("B", 4, 5, "Magic Reflection", "In Jux Sanct", ("GL", "MR", "SS")),
    ("H", 5, 5, "Mind Blast", "Por Corp Wis", ("BP", "MR", "NS", "SA")),
    ("H", 6, 5, "Paralyze", "An Ex Por", ("GL", "MR", "SS")),
    ("N", 7, 5, "Poison Field", "In Nox Grav", ("BP", "NS", "SS")),
    ("N", 8, 5, "Summon Creature", "Kal Xen", ("BP", "MR", "SS")),
    # ── Magery Circle 6 ──
    ("N", 1, 6, "Dispel", "An Ort", ("GL", "MR", "SA")),
    ("H", 2, 6, "Energy Bolt", "Corp Por", ("BP", "NS")),
    ("H", 3, 6, "Explosion", "Vas Ort Flam", ("BP", "MR")),
    ("N", 4, 6, "Invisibility", "An Lor Xen", ("BP", "NS")),
    ("N", 5, 6, "Mark", "Kal Por Ylem", ("BP", "BP", "MR")),
    ("N", 6, 6, "Mass Curse", "Vas Des Sanct", ("GL", "MR", "NS", "SA")),
    ("N", 7, 6, "Paralyze Field", "In Ex Grav", ("BP", "GS", "SS")),
    ("N", 8, 6, "Reveal", "Wis Quas", ("BP", "SA")),
    # ── Magery Circle 7 ──
    ("H", 1, 7, "Chain Lightning", "Vas Ort Grav", ("BP", "MR", "BP", "SA")),
    ("N", 2, 7, "Energy Field", "In Sanct Grav", ("BP", "MR", "SS", "SA")),
    ("H", 3, 7, "Flamestrike", "Kal Vas Flam", ("SS", "SA")),
    ("N", 4, 7, "Gate Travel", "Vas Rel Por", ("BP", "MR", "SA")),
    ("H", 5, 7, "Mana Vampire", "Ort Sanct", ("BP", "BP", "MR", "SS")),
    ("N", 6, 7, "Mass Dispel", "Vas An Ort", ("BP", "GL", "MR", "SA")),
    ("H", 7, 7, "Meteor Swarm", "Flam Kal Des Ylem", ("BP", "SS", "MR", "SA")),
    ("N", 8, 7, "Polymorph", "Vas Ylem Rel", ("BP", "MR", "SS")),
    # ── Magery Circle 8 ──
    ("H", 1, 8, "Earthquake", "In Vas Por", ("BP", "GS", "MR", "SA")),
    ("H", 2, 8, "Energy Vortex", "Vas Corp Por", ("BP", "BP", "MR", "NS")),
    ("B", 3, 8, "Resurrection", "An Corp", ("BP", "GL", "GS")),
    ("N", 4, 8, "Summon Air Elemental", "Kal Vas Xen Hur", ("BP", "MR", "SS")),
    ("N", 5, 8, "Summon Daemon", "Kal Vas Xen Corp", ("BP", "MR", "SS", "SA")),
    ("N", 6, 8, "Summon Earth Elemental", "Kal Vas Xen Ylem", ("BP", "MR", "SS")),
    ("N", 7, 8, "Summon Fire Elemental", "Kal Vas Xen Flam", ("BP", "MR", "SS", "SA")),
    ("N", 8, 8, "Summon Water Elemental", "Kal Vas Xen An Flam", ("BP", "MR", "SS")),
    # ── Necromancy (circle 10) ──
    ("N", 1, 10, "Animate Dead", "Uus Corp", ("DB", "GD")),
    ("H", 2, 10, "Blood Oath", "In Jux Mani Xen", ("DB",)),
    ("N", 3, 10, "Corpse Skin", "In Agle Corp Ylem", ("BW", "GD")),
    ("B", 4, 10, "Curse Weapon", "An Sanct Gra Char", ("PI",)),
    ("H", 5, 10, "Evil Omen", "Pas Tym An Sanct", ("BW", "NC")),
    ("N", 6, 10, "Horrific Beast", "Rel Xen Vas Bal", ("BW", "DB")),
    ("N", 7, 10, "Lich Form", "Rel Xen Corp Ort", ("NC", "DB", "GD")),
    ("H", 8, 10, "Mind Rot", "Wis An Ben", ("BW", "DB", "PI")),
    ("H", 9, 10, "Pain Spike", "In Sar", ("GD", "PI")),
    ("H", 10, 10, "Poison Strike", "In Vas Nox", ("NC",)),
    ("H", 11, 10, "Strangle", "In Bal Nox", ("NC", "DB")),
    ("N", 12, 10, "Summon Familiar", "Kal Xen Bal", ("BW", "GD", "DB")),
    ("N", 13, 10, "Vampiric Embrace", "Rel Xen An Sanct", ("BW", "NC", "PI")),
    ("H", 14, 10, "Vengeful Spirit", "Kal Xen Bal Beh", ("BW", "GD", "PI")),
    ("H", 15, 10, "Wither", "Kal Vas An Flam", ("NC", "GD", "PI")),
    ("N", 16, 10, "Wraith Form", "Rel Xen Um", ("PI", "NC")),
    ("N", 17, 10, "Exorcism", "Ort Corp Grav", ("NC", "GD")),
    # ── Chivalry (circle 20) ──
    ("B", 1, 20, "Cleanse by Fire", "Expor Flamus", ()),
    ("B", 2, 20, "Close Wounds", "Obsu Vulni", ()),
    ("B", 3, 20, "Consecrate Weapon", "Consecrus Arma", ()),
    ("N", 4, 20, "Dispel Evil", "Dispiro Malas", ()),
    ("B", 5, 20, "Divine Fury", "Divinum Furis", ()),
    ("H", 6, 20, "Enemy of One", "Forul Solum", ()),
    ("H", 7, 20, "Holy Light", "Augus Luminos", ()),
    ("B", 8, 20, "Noble Sacrifice", "Dium Prostra", ()),
    ("B", 9, 20, "Remove Curse", "Extermo Vomica", ()),
    ("N", 10, 20, "Sacred Journey", "Sanctum Viatas", ()),
    # ── Bushido (circle 40) ──
    ("H", 1, 40, "Honorable Execution", "", ()),
    ("B", 2, 40, "Confidence", "", ()),
    ("B", 3, 40, "Evasion", "", ()),
    ("H", 4, 40, "Counter Attack", "", ()),
    ("H", 5, 40, "Lightning Strike", "", ()),
    ("H", 6, 40, "Momentum Strike", "", ()),
    # ── Ninjitsu (circle 50) ──
    ("H", 1, 50, "Focus Attack", "", ()),
    ("H", 2, 50, "Death Strike", "", ()),
    ("B", 3, 50, "Animal Form", "", ()),
    ("H", 4, 50, "Ki Attack", "", ()),
    ("H", 5, 50, "Surprise Attack", "", ()),
    ("H", 6, 50, "Backstab", "", ()),
    ("N", 7, 50, "Shadowjump", "", ()),
    ("N", 8, 50, "Mirror Image", "", ()),
    # ── Spellweaving (circle 60) ──
    ("N", 1, 60, "Arcane Circle", "Myrshalee", ()),
    ("B", 2, 60, "Gift of Renewal", "Olorisstra", ()),
    ("N", 3, 60, "Immolating Weapon", "Thalshara", ()),
    ("H", 4, 60, "Attune Weapon", "Haeldril", ()),
    ("H", 5, 60, "Thunderstorm", "Erelonia", ()),
    ("H", 6, 60, "Nature's Fury", "Rauvvrae", ()),
    ("N", 7, 60, "Summon Fey", "Alalithra", ()),
    ("N", 8, 60, "Summon Fiend", "Nylisstra", ()),
    ("N", 9, 60, "Reaper Form", "Tarisstree", ()),
    ("H", 10, 60, "Wildfire", "Haelyn", ()),
    ("H", 11, 60, "Essence of Wind", "Anathrae", ()),
    ("N", 12, 60, "Dryad Allure", "Rathril", ()),
    ("N", 13, 60, "Ethereal Voyage", "Orlavdra", ()),
    ("H", 14, 60, "Word of Death", "Nyraxle", ()),
    ("B", 15, 60, "Gift of Life", "Illorae", ()),
    ("B", 16, 60, "Arcane Empowerment", "Aslavdra", ()),
    # ── Mysticism (circle 65) ──
    ("H", 28, 65, "Nether Bolt", "In Corp Ylem", ()),
    ("N", 29, 65, "Healing Stone", "Kal In Mani", ()),
    ("H", 30, 65, "Purge Magic", "An Ort Sanct", ()),
    ("N", 31, 65, "Enchant", "In Ort Ylem", ()),
    ("H", 32, 65, "Sleep", "In Zu", ()),
    ("H", 33, 65, "Eagle Strike", "Kal Por Xen", ()),
    ("H", 34, 65, "Animated Weapon", "In Jux Por Ylem", ()),
    ("N", 35, 65, "Stone Form", "In Rel Ylem", ()),
    ("N", 36, 65, "Spell Trigger", "In Vas Ort Ex", ()),
    ("H", 37, 65, "Mass Sleep", "Vas Zu", ()),
    ("B", 38, 65, "Cleansing Winds", "In Vas Mani Hur", ()),
    ("H", 39, 65, "Bombard", "Corp Por Ylem", ()),
    ("H", 40, 65, "Spell Plague", "Vas Rel Jux Ort", ()),
    ("H", 41, 65, "Hail Storm", "Kal Des Ylem", ()),
    ("H", 42, 65, "Nether Cyclone", "Grav Hur", ()),
    ("H", 43, 65, "Rising Colossus", "Kal Vas Xen Corp Ylem", ()),
]
# fmt: on

# Build the spell registry
SPELLS: dict[int, SpellDef] = {}
SPELLS_BY_NAME: dict[str, SpellDef] = {}

for _flag, _num, _circ, _name, _words, _reags in _RAW_SPELLS:
    _sid = _spell_id(_num, _circ)
    _spell = SpellDef(
        id=_sid,
        number=_num,
        circle=_circ,
        name=_name,
        words_of_power=_words,
        flag=_parse_flag(_flag),
        school=_school_from_circle(_circ),
        reagents=_reags,
    )
    SPELLS[_sid] = _spell
    SPELLS_BY_NAME[_name.lower()] = _spell


# ── Helper functions ──


def get_spell(spell_id: int) -> SpellDef | None:
    """Look up a spell by its numeric ID."""
    return SPELLS.get(spell_id)


def get_spell_by_name(name: str) -> SpellDef | None:
    """Look up a spell by name (case-insensitive)."""
    return SPELLS_BY_NAME.get(name.lower())


def spells_by_school(school: SpellSchool) -> list[SpellDef]:
    """Return all spells in a given school, sorted by ID."""
    return sorted(
        (s for s in SPELLS.values() if s.school == school),
        key=lambda s: s.id,
    )


def harmful_spells(school: SpellSchool | None = None) -> list[SpellDef]:
    """Return all harmful spells, optionally filtered by school."""
    return [
        s for s in SPELLS.values()
        if s.flag == SpellFlag.HARMFUL
        and (school is None or s.school == school)
    ]


def beneficial_spells(school: SpellSchool | None = None) -> list[SpellDef]:
    """Return all beneficial spells, optionally filtered by school."""
    return [
        s for s in SPELLS.values()
        if s.flag == SpellFlag.BENEFICIAL
        and (school is None or s.school == school)
    ]


def healing_spells() -> list[SpellDef]:
    """Return spells commonly used for healing."""
    names = {"heal", "greater heal", "cure", "arch cure", "close wounds",
             "cleanse by fire", "cleansing winds"}
    return [s for s in SPELLS.values() if s.name.lower() in names]


# ── Reagent accounting ──


def reagent_costs(spell: SpellDef) -> dict[str, int]:
    """Per-cast reagent requirement as {name: count}.

    ``SpellDef.reagents`` is a tuple of abbreviations in which a reagent may
    appear MORE THAN ONCE — e.g. Recall is ``("BP", "BP", "MR")`` (2 Black
    Pearl + 1 Mandrake Root) and Chain Lightning needs 2 Black Pearl too. A
    naive ``set(reagents)`` check would treat "I have 1 Black Pearl" as enough
    and let the cast be attempted anyway. Counting occurrences and keying by
    the full reagent NAME (BM/BP both map to "Black Pearl") gives the true
    per-cast cost.
    """
    costs: dict[str, int] = {}
    for abbr in spell.reagents:
        name = REAGENT_NAMES.get(abbr, abbr)
        costs[name] = costs.get(name, 0) + 1
    return costs


def missing_reagents(
    spell: SpellDef, available: dict[int, int]
) -> list[str]:
    """Reagent names for which ``available`` is short of one cast.

    ``available`` maps reagent item graphic id → on-hand quantity (e.g. from
    counting the backpack). Returns the reagent names whose on-hand count is
    below the per-cast requirement, sorted for deterministic messaging. An
    empty list means the spell can be cast at least once.

    Reagent-free schools (Chivalry/Bushido/etc. have ``reagents=()``) trivially
    return ``[]``.
    """
    short: list[str] = []
    for name, need in reagent_costs(spell).items():
        graphic = REAGENT_GRAPHICS.get(name)
        # Unknown graphic (no inventory mapping) → can't prove we have it;
        # treat as not-missing so we never block on bookkeeping gaps.
        if graphic is None:
            continue
        if available.get(graphic, 0) < need:
            short.append(name)
    return sorted(short)


def has_reagents_for(spell: SpellDef, available: dict[int, int]) -> bool:
    """True when ``available`` covers at least one cast of ``spell``."""
    return not missing_reagents(spell, available)
