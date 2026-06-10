"""UO domain constants for the Foundry kernel — independent of anima/.

These are facts about the Ultima Online protocol and ruleset (skill numbering,
item graphics, packet ids), not agent behavior. They live in the kernel because
fitness/descriptor computation depends on them and they must not be mutator-
editable. Values are deliberately conservative; tune in Phase 0.
"""

from __future__ import annotations

# --- Skills -----------------------------------------------------------------
# Standard UO skill id -> name (0..57, UOR-era + later).
SKILL_NAMES: dict[int, str] = {
    0: "Alchemy", 1: "Anatomy", 2: "Animal Lore", 3: "Item ID", 4: "Arms Lore",
    5: "Parrying", 6: "Begging", 7: "Blacksmithy", 8: "Bowcraft", 9: "Peacemaking",
    10: "Camping", 11: "Carpentry", 12: "Cartography", 13: "Cooking",
    14: "Detect Hidden", 15: "Discordance", 16: "Eval Int", 17: "Healing",
    18: "Fishing", 19: "Forensics", 20: "Herding", 21: "Hiding", 22: "Provocation",
    23: "Inscription", 24: "Lockpicking", 25: "Magery", 26: "Resist Spells",
    27: "Tactics", 28: "Snooping", 29: "Musicianship", 30: "Poisoning",
    31: "Archery", 32: "Spirit Speak", 33: "Stealing", 34: "Tailoring",
    35: "Animal Taming", 36: "Taste ID", 37: "Tinkering", 38: "Tracking",
    39: "Veterinary", 40: "Swordsmanship", 41: "Mace Fighting", 42: "Fencing",
    43: "Wrestling", 44: "Lumberjacking", 45: "Mining", 46: "Meditation",
    47: "Stealth", 48: "Remove Trap", 49: "Necromancy", 50: "Focus",
    51: "Chivalry", 52: "Bushido", 53: "Ninjitsu", 54: "Spellweaving",
    55: "Mysticism", 56: "Imbuing", 57: "Throwing",
}

# Behavior-descriptor profession categories (FOUNDRY.md §4 profession_focus).
GATHERING = "GATHERING"
CRAFTING = "CRAFTING"
COMBAT = "COMBAT"
MAGIC = "MAGIC"
BARD_SOCIAL = "BARD-SOCIAL"
THIEF_STEALTH = "THIEF-STEALTH"
NONE = "NONE"

PROFESSION_BINS: tuple[str, ...] = (
    GATHERING, CRAFTING, COMBAT, MAGIC, BARD_SOCIAL, THIEF_STEALTH, NONE,
)

# Map skill id -> profession category. Uncategorized skills map to None here and
# do not contribute to profession_focus (they are not livelihood-defining).
SKILL_CATEGORY: dict[int, str] = {
    # Gathering
    18: GATHERING, 44: GATHERING, 45: GATHERING,
    # Crafting
    0: CRAFTING, 7: CRAFTING, 8: CRAFTING, 11: CRAFTING, 12: CRAFTING,
    13: CRAFTING, 34: CRAFTING, 37: CRAFTING, 56: CRAFTING,
    # Combat
    1: COMBAT, 5: COMBAT, 17: COMBAT, 27: COMBAT, 31: COMBAT, 40: COMBAT,
    41: COMBAT, 42: COMBAT, 43: COMBAT, 51: COMBAT, 52: COMBAT, 57: COMBAT,
    # Magic
    16: MAGIC, 23: MAGIC, 25: MAGIC, 26: MAGIC, 32: MAGIC, 46: MAGIC,
    49: MAGIC, 54: MAGIC, 55: MAGIC,
    # Bard / social
    6: BARD_SOCIAL, 9: BARD_SOCIAL, 15: BARD_SOCIAL, 22: BARD_SOCIAL, 29: BARD_SOCIAL,
    # Thief / stealth
    21: THIEF_STEALTH, 24: THIEF_STEALTH, 28: THIEF_STEALTH, 30: THIEF_STEALTH,
    33: THIEF_STEALTH, 47: THIEF_STEALTH, 48: THIEF_STEALTH, 53: THIEF_STEALTH,
}

# --- Item valuation ---------------------------------------------------------
# Graphic id -> rough gold value, for produce_term (FOUNDRY.md §5, weight 0.2).
# Conservative starting table; unknown graphics default to ITEM_VALUE_DEFAULT.
# Keyed by base graphic; gathered/crafted goods the early personas produce.
ITEM_VALUES: dict[int, int] = {
    0x1BDD: 3,   # Log
    0x1BE0: 3,   # Log (variant)
    0x1BD7: 3,   # Board
    0x19B7: 5,   # Ore (small pile)
    0x19B8: 5,   # Ore
    0x19B9: 5,   # Ore
    0x19BA: 5,   # Ore (large pile)
    0x1BEF: 6,   # Ingot
    0x1BF0: 6,   # Ingot
    0x1BF1: 6,   # Ingot
    0x1BF2: 6,   # Iron ingot
    0x0F8F: 0,   # (placeholder) gem — keep 0 until valued
}
ITEM_VALUE_DEFAULT = 1
GOLD_GRAPHIC = 0x0EED

# --- Layers -----------------------------------------------------------------
LAYER_BACKPACK = 0x15  # 21

# --- Movement directions (low 3 bits of the walk-request flag byte) ---------
# dir -> (dx, dy). UO: 0=N,1=NE,2=E,3=SE,4=S,5=SW,6=W,7=NW.
DIR_DELTA: dict[int, tuple[int, int]] = {
    0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1),
    4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1),
}

# --- Client -> Server packet classification (the agent's "actions") ---------
# Meaningful intent packets that count toward the descriptor's action
# denominator. Housekeeping/polling packets (ping 0x73, status req 0x34, OPL req
# 0xD6, general-info 0xBF, client-special 0xF0) are intentionally excluded.
CS_MOVE = 0x02
CS_ATTACK = 0x05
CS_USE = 0x06          # double-click / use
CS_PICKUP = 0x07       # lift item
CS_DROP = 0x08         # drop item
CS_CLICK = 0x09        # single-click
CS_SPEECH_ASCII = 0x03
CS_SPEECH_UNICODE = 0xAD
CS_GUMP_REPLY = 0xB1
CS_TARGET = 0x6C
CS_VENDOR_BUY = 0x3B
CS_VENDOR_SELL = 0x9F
CS_EQUIP_REQ = 0x13
CS_TEXT_COMMAND = 0x12  # UseSkill ("skill id 0") / CastSpell ("spell id")

# Which C->S pids count as a deliberate action, grouped for descriptor fractions.
ACTION_GROUP: dict[int, str] = {
    CS_MOVE: "move",
    CS_ATTACK: "attack",
    CS_USE: "use",
    CS_PICKUP: "use",
    CS_DROP: "use",
    CS_SPEECH_ASCII: "speech",
    CS_SPEECH_UNICODE: "speech",
    CS_GUMP_REPLY: "use",
    CS_VENDOR_BUY: "trade",
    CS_VENDOR_SELL: "trade",
    CS_EQUIP_REQ: "use",
    # UseSkill/CastSpell are as deliberate as a tool double-click.
    # Omitting 0x12 made skill-only professions (Hiding, Meditation,
    # Magery grinds) read as frozen — liveness gate collapse — and
    # inflated their sociability to speech/speech. The target reply
    # (0x6C) stays excluded so use→target flows don't double-count.
    CS_TEXT_COMMAND: "skill",
}
