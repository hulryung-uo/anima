"""Name generators for UO-themed account and character names."""

from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# Account name generation: UO role/concept + 4 digits
# ---------------------------------------------------------------------------

_ACCOUNT_WORDS = [
    "Paladin", "Ranger", "Mage", "Bard", "Warrior", "Healer",
    "Alchemist", "Tinker", "Scribe", "Tamer", "Necro", "Thief",
    "Mystic", "Druid", "Knight", "Sage", "Monk", "Rogue",
    "Warden", "Nomad", "Seeker", "Pilgrim", "Herald", "Squire",
    "Avatar", "Wanderer", "Guardian", "Sentinel", "Templar",
    "Corsair", "Reaver", "Conjurer", "Invoker", "Diviner",
]


def generate_account_name() -> str:
    """Generate a UO-themed account name: word + 4 digits."""
    word = random.choice(_ACCOUNT_WORDS)
    digits = random.randint(1000, 9999)
    return f"{word}{digits}"


# ---------------------------------------------------------------------------
# Character name generation: first name + surname
# ---------------------------------------------------------------------------

_FIRST_NAMES = [
    # Male-leaning
    "Aldric", "Balen", "Cedric", "Doran", "Elric", "Fenwick",
    "Gareth", "Hadric", "Ivor", "Jareth", "Kael", "Leoric",
    "Mordain", "Nolan", "Osric", "Perin", "Rolan", "Soren",
    "Thalos", "Ulric", "Varen", "Wulfric", "Xander", "Yorick",
    # Female-leaning
    "Aria", "Brenna", "Calla", "Deira", "Elara", "Freya",
    "Gwyn", "Helia", "Iona", "Jessa", "Kira", "Lyra",
    "Maren", "Nessa", "Orla", "Petra", "Ravenna", "Sera",
    "Thora", "Una", "Vala", "Wren", "Yara", "Zara",
]

_SURNAMES = [
    # Nature / place
    "Ashford", "Blackwood", "Deepwell", "Elmsworth", "Foxmere",
    "Greenvale", "Hawkridge", "Ironbark", "Lakemoor", "Nighthollow",
    "Oakvale", "Ravenhill", "Stonecrest", "Thornwall", "Windhaven",
    # Craft / virtue
    "Brightblade", "Dawnforge", "Goldweaver", "Grimhammer", "Hearthstone",
    "Lightbringer", "Moonwhisper", "Shadowmend", "Silverthorn", "Starwarden",
    "Trueguard", "Valorborn", "Wyrmsbane", "Spellwright", "Duskwalker",
    # Britannia-flavored
    "of Britain", "of Trinsic", "of Vesper", "of Minoc", "of Yew",
    "of Moonglow", "of Jhelom", "of Skara Brae", "of Magincia",
]


def generate_character_name() -> str:
    """Generate a UO-themed character name: first + surname."""
    first = random.choice(_FIRST_NAMES)
    surname = random.choice(_SURNAMES)
    return f"{first} {surname}"
