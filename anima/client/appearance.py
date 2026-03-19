"""Character appearance data and randomized character creation.

Based on ClassicUO CharacterCreationValues.cs and CreateCharAppearanceGump.cs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from anima.client.codec import PacketWriter

# ---------------------------------------------------------------------------
# Valid appearance values from ClassicUO
# ---------------------------------------------------------------------------

# fmt: off

# Human skin tones (from CharacterCreationValues.cs)
HUMAN_SKIN_TONES = [
    0x03EA, 0x03EB, 0x03EC, 0x03ED, 0x03EE, 0x03EF, 0x03F0, 0x03F1,
    0x03F2, 0x03F3, 0x03F4, 0x03F5, 0x03F6, 0x03F7, 0x03F8, 0x03F9,
    0x03FA, 0x03FB, 0x03FC, 0x03FD, 0x03FE, 0x03FF, 0x0400, 0x0401,
    0x0402, 0x0403, 0x0404, 0x0405, 0x0406, 0x0407, 0x0408, 0x0409,
    0x040A, 0x040B, 0x040C, 0x040D, 0x040E, 0x040F, 0x0410, 0x0411,
    0x0412, 0x0413, 0x0414, 0x0415, 0x0416, 0x0417, 0x0418, 0x0419,
    0x041A, 0x041B, 0x041C, 0x041D, 0x041E, 0x041F, 0x0420, 0x0421,
]

# Human hair colors
HUMAN_HAIR_HUES = [
    0x044D, 0x0455, 0x045D, 0x0465, 0x046D, 0x0475, 0x044E, 0x0456,
    0x045E, 0x0466, 0x046E, 0x0476, 0x044F, 0x0457, 0x045F, 0x0467,
    0x046F, 0x0477, 0x0450, 0x0458, 0x0460, 0x0468, 0x0470, 0x0478,
    0x0451, 0x0459, 0x0461, 0x0469, 0x0471, 0x0479, 0x0452, 0x045A,
    0x0462, 0x046A, 0x0472, 0x047A, 0x0453, 0x045B, 0x0463, 0x046B,
    0x0473, 0x047B, 0x0454, 0x045C, 0x0464, 0x046C, 0x0474, 0x047C,
]

# Human male hair styles (graphic IDs, 0 = bald)
HUMAN_MALE_HAIR = [0, 0x203B, 0x203C, 0x203D, 0x2044, 0x2045, 0x204A, 0x2047, 0x2048, 0x2049]

# Human female hair styles
HUMAN_FEMALE_HAIR = [0, 0x203B, 0x203C, 0x203D, 0x2044, 0x2045, 0x204A, 0x2047, 0x2049, 0x2046]

# Human male facial hair (0 = none)
HUMAN_MALE_FACIAL_HAIR = [0, 0x2040, 0x203E, 0x203F, 0x2041, 0x204B, 0x204C, 0x204D]

# Clothing hue range (common range used in character creation)
CLOTHING_HUES = list(range(0x0002, 0x03E9, 5))  # ~200 colors

# fmt: on


# ---------------------------------------------------------------------------
# Persona-to-creation mapping
# ---------------------------------------------------------------------------

# Stats: (str, dex, int), total must equal 80
PERSONA_STATS: dict[str, tuple[int, int, int]] = {
    "adventurer": (35, 25, 20),
    "blacksmith":  (60, 10, 10),
    "merchant":    (25, 25, 30),
    "mage":        (10, 10, 60),
    "bard":        (15, 30, 35),
    "ranger":      (40, 30, 10),
}

# Initial skills: list of (skill_id, value), up to 4 skills, values should sum to 100
# UO Skill IDs: 0=Alchemy, 8=Blacksmith, 11=Carpentry, 13=Cooking, 17=Healing,
# 18=Fishing, 25=Magery, 27=Meditation, 31=Archery, 37=Tinkering,
# 40=Swordsmanship, 41=MaceFighting, 42=Fencing, 44=Lumberjacking, 45=Mining,
# 46=Musicianship, 48=Peacemaking, 53=Tactics, 57=Tailoring
PERSONA_SKILLS: dict[str, list[tuple[int, int]]] = {
    "adventurer": [(40, 50), (17, 50), (0, 0), (0, 0)],     # Swordsmanship, Healing
    "blacksmith":  [(45, 50), (8, 50), (0, 0), (0, 0)],      # Mining, Blacksmith
    "merchant":    [(37, 50), (57, 50), (0, 0), (0, 0)],      # Tinkering, Tailoring
    "mage":        [(25, 50), (27, 50), (0, 0), (0, 0)],      # Magery, Meditation
    "bard":        [(46, 50), (48, 50), (0, 0), (0, 0)],      # Musicianship, Peacemaking
    "ranger":      [(31, 50), (44, 50), (0, 0), (0, 0)],      # Archery, Lumberjacking
}


@dataclass
class CharacterAppearance:
    """Character appearance settings for creation."""

    name: str = "Anima"
    female: bool = False
    skin_hue: int = 0x03EA
    hair_style: int = 0x203B
    hair_hue: int = 0x044D
    facial_hair_style: int = 0  # 0 = none
    facial_hair_hue: int = 0x044D
    shirt_hue: int = 0x0002
    pants_hue: int = 0x0002
    strength: int = 60
    dexterity: int = 10
    intelligence: int = 10
    city_index: int = 0  # 0=New Haven, 3=Britain
    # 4 skills: [(skill_id, value), ...]
    skills: list[tuple[int, int]] = field(
        default_factory=lambda: [(0, 50), (1, 50), (2, 0), (3, 0)]
    )

    @staticmethod
    def random(name: str = "Anima", city_index: int = 0) -> CharacterAppearance:
        """Generate a random human appearance with random stats."""
        female = random.choice([True, False])
        skin_hue = random.choice(HUMAN_SKIN_TONES)
        hair_hue = random.choice(HUMAN_HAIR_HUES)

        if female:
            hair_style = random.choice(HUMAN_FEMALE_HAIR)
            facial_hair_style = 0
            facial_hair_hue = 0
        else:
            hair_style = random.choice(HUMAN_MALE_HAIR)
            facial_hair_style = random.choice(HUMAN_MALE_FACIAL_HAIR)
            facial_hair_hue = hair_hue  # match hair color

        shirt_hue = random.choice(CLOTHING_HUES)
        pants_hue = random.choice(CLOTHING_HUES)

        # Randomize stats (total = 80)
        strength = random.randint(10, 60)
        remaining = 80 - strength
        dexterity = random.randint(10, min(60, remaining - 10))
        intelligence = remaining - dexterity

        return CharacterAppearance(
            name=name,
            female=female,
            skin_hue=skin_hue,
            hair_style=hair_style,
            hair_hue=hair_hue,
            facial_hair_style=facial_hair_style,
            facial_hair_hue=facial_hair_hue,
            shirt_hue=shirt_hue,
            pants_hue=pants_hue,
            strength=strength,
            dexterity=dexterity,
            intelligence=intelligence,
            city_index=city_index,
        )

    @staticmethod
    def from_persona(
        persona_name: str,
        character_name: str = "Anima",
        city_index: int = 0,
    ) -> CharacterAppearance:
        """Create appearance based on persona with appropriate stats/skills.

        Appearance (gender, hair, skin, clothes) is randomized.
        Stats and skills are determined by the persona type.
        """
        # Start with random appearance
        app = CharacterAppearance.random(name=character_name, city_index=city_index)

        # Apply persona-specific stats
        if persona_name in PERSONA_STATS:
            app.strength, app.dexterity, app.intelligence = PERSONA_STATS[persona_name]

        # Apply persona-specific skills
        if persona_name in PERSONA_SKILLS:
            app.skills = list(PERSONA_SKILLS[persona_name])

        return app


# ---------------------------------------------------------------------------
# Some preset templates (kept for backward compatibility)
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, CharacterAppearance] = {
    "warrior": CharacterAppearance(
        female=False, skin_hue=0x03F5, hair_style=0x2048, hair_hue=0x0455,
        facial_hair_style=0x204D, facial_hair_hue=0x0455,
        shirt_hue=0x0037, pants_hue=0x004C,
        strength=60, dexterity=10, intelligence=10,
        skills=[(40, 50), (53, 50), (0, 0), (0, 0)],  # Swordsmanship, Tactics
    ),
    "mage": CharacterAppearance(
        female=True, skin_hue=0x03EA, hair_style=0x2046, hair_hue=0x0474,
        facial_hair_style=0, facial_hair_hue=0,
        shirt_hue=0x0010, pants_hue=0x0010,
        strength=10, dexterity=10, intelligence=60,
        skills=[(25, 50), (27, 50), (0, 0), (0, 0)],  # Magery, Meditation
    ),
    "smith": CharacterAppearance(
        female=False, skin_hue=0x0410, hair_style=0x203C, hair_hue=0x046A,
        facial_hair_style=0x203E, facial_hair_hue=0x046A,
        shirt_hue=0x0224, pants_hue=0x0156,
        strength=60, dexterity=10, intelligence=10,
        skills=[(45, 50), (8, 50), (0, 0), (0, 0)],  # Mining, Blacksmith
    ),
    "merchant": CharacterAppearance(
        female=False, skin_hue=0x03F0, hair_style=0x2044, hair_hue=0x0460,
        facial_hair_style=0, facial_hair_hue=0,
        shirt_hue=0x01A2, pants_hue=0x0070,
        strength=30, dexterity=25, intelligence=25,
        skills=[(37, 50), (57, 50), (0, 0), (0, 0)],  # Tinkering, Tailoring
    ),
    "ranger": CharacterAppearance(
        female=True, skin_hue=0x0400, hair_style=0x2049, hair_hue=0x0452,
        facial_hair_style=0, facial_hair_hue=0,
        shirt_hue=0x0182, pants_hue=0x017C,
        strength=40, dexterity=30, intelligence=10,
        skills=[(31, 50), (44, 50), (0, 0), (0, 0)],  # Archery, Lumberjacking
    ),
}


def build_create_character(appearance: CharacterAppearance, slot: int = 0) -> bytes:
    """Build CreateCharacter packet (0xF8, 106 bytes).

    Packet format based on ClassicUO OutgoingPackets.cs Send_CreateCharacter70.
    """
    w = PacketWriter()
    w.write_u8(0xF8)
    w.write_u32(0xEDEDEDED)  # pattern1
    w.write_u32(0xFFFFFFFF)  # pattern2
    w.write_u8(0x00)         # pattern3

    w.write_ascii(appearance.name, 30)
    w.write_zeros(2)         # unknown

    w.write_u32(0x00000000)  # client flags
    w.write_u32(0x00000001)  # unknown (ClassicUO sends 1)
    w.write_u32(0x00000000)  # login count

    w.write_u8(0)            # profession (0 = custom)
    w.write_zeros(15)        # reserved

    # Gender+Race encoding: (race-1)*2 + (female?1:0), human race=1
    gender_race = 0 + (1 if appearance.female else 0)
    w.write_u8(gender_race)

    # Stats
    w.write_u8(appearance.strength)
    w.write_u8(appearance.dexterity)
    w.write_u8(appearance.intelligence)

    # Skills (4 pairs: skill_id u8, value u8)
    skills = appearance.skills[:4]
    while len(skills) < 4:
        skills.append((0, 0))
    for skill_id, skill_val in skills:
        w.write_u8(skill_id)
        w.write_u8(skill_val)

    # Appearance
    w.write_u16(appearance.skin_hue)
    w.write_u16(appearance.hair_style)
    w.write_u16(appearance.hair_hue)
    w.write_u16(appearance.facial_hair_style)
    w.write_u16(appearance.facial_hair_hue)

    # City & slot
    w.write_u16(appearance.city_index)
    w.write_zeros(2)         # padding
    w.write_u16(slot)
    w.write_u32(0x7F000001)  # client IP

    # Clothing colors
    w.write_u16(appearance.shirt_hue)
    w.write_u16(appearance.pants_hue)

    data = w.to_bytes()
    # Pad or trim to exactly 106 bytes
    if len(data) < 106:
        data = data + b"\x00" * (106 - len(data))
    return data[:106]
