"""Vendor knowledge — which vendor types buy which items.

Based on ServUO SBInfo vendor buy lists.
Used by planner to choose the right vendor when selling.
"""

# Graphic → list of vendor keywords (from Common.map location names)
# that will buy this item type
ITEM_VENDOR_MAP: dict[int, list[str]] = {}

# Bladed weapons → blacksmith, weaponsmith, arms
_BLADED = [0x1441, 0x13FF, 0x13B6, 0x0F5E, 0x0F61, 0x13B8, 0x13B9, 0x1401]
for g in _BLADED:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Axes → blacksmith, weaponsmith
_AXES = [0x0F49, 0x0F47, 0x0F4B, 0x13FA, 0x1443]
for g in _AXES:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Ringmail → blacksmith, armorer
_RINGMAIL = [0x13EB, 0x13EC, 0x13EE, 0x13F0]
for g in _RINGMAIL:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Chainmail → blacksmith, armorer
_CHAINMAIL = [0x13BF, 0x13C0, 0x13C4]
for g in _CHAINMAIL:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Platemail → blacksmith, armorer
_PLATEMAIL = [0x1408, 0x1410, 0x1411, 0x1413, 0x1414, 0x1415]
for g in _PLATEMAIL:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Ingots → blacksmith only (all stack-size graphics)
ITEM_VENDOR_MAP[0x1BF2] = ["blacksmith"]
ITEM_VENDOR_MAP[0x1BEF] = ["blacksmith"]
ITEM_VENDOR_MAP[0x1BF0] = ["blacksmith"]
ITEM_VENDOR_MAP[0x1BF1] = ["blacksmith"]

# Shields → blacksmith, armorer
_SHIELDS = [0x1B72, 0x1B73, 0x1B74, 0x1B76, 0x1B78, 0x1B7A, 0x1B7B]
for g in _SHIELDS:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Daggers → blacksmith, arms (common starting weapons)
_DAGGERS = [0x0F51, 0x0F52]
for g in _DAGGERS:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "arms"]

# Clothing → tailor (shirts, kilts, half aprons, shoes, etc.)
_CLOTHING = [
    0x1517, 0x1518,  # shirt
    0x1537, 0x1538,  # kilt
    0x153B, 0x153C,  # half apron
    0x170F, 0x1710,  # shoes
    0x1515, 0x1516,  # cloak
    0x1531, 0x1532,  # skirt
    0x1539, 0x153A,  # body sash
    0x1541, 0x1542,  # surcoat
    0x1EFD, 0x1EFE,  # fancy shirt
    0x1F01, 0x1F02,  # tunic
    0x1F03, 0x1F04,  # fancy dress
    0x1F7B, 0x1F7C,  # doublet
    0x1F9F, 0x1FA0,  # robe
    0x152E, 0x152F,  # bandana
    0x1544, 0x1545,  # feathered hat
    0x1713, 0x1714,  # sandals
    0x1711, 0x1712,  # boots
]
for g in _CLOTHING:
    ITEM_VENDOR_MAP[g] = ["tailor"]

# Candles, torches → provisioner (SBProvisioner buy list)
_PROVISIONS = [
    0x0A28, 0x0A0F, 0x0A10, 0x0A11,  # candles
    0x0A26,  # candle
    0x0F6B, 0x0F64,  # torches
]
for g in _PROVISIONS:
    ITEM_VENDOR_MAP[g] = ["provisioner"]

# Books, scrolls → mage, provisioner
_BOOKS = [0x0FEF, 0x0FF0, 0x0FF1, 0x0FF2, 0x0FF3, 0x0FF4, 0x0FBD, 0x0FBE]
for g in _BOOKS:
    ITEM_VENDOR_MAP[g] = ["mage", "provisioner"]

# Crafting tools — normally in KEEP_GRAPHICS, but surplus (count ≥ 2) is
# sellable via sell_to_vendor's "keep-at-least-1" logic.  Mapping them
# here lets the planner pick the correct vendor type for a bulk sell
# (e.g. tinker's tools → tinker guildmistress) instead of falling back
# to "blacksmith" and getting refused.
_TINKER_TOOLS = [0x1EB8, 0x1EBC]
for g in _TINKER_TOOLS:
    ITEM_VENDOR_MAP[g] = ["tinker", "blacksmith"]

_TONGS = [0x0FBB, 0x0FBC]
for g in _TONGS:
    ITEM_VENDOR_MAP[g] = ["blacksmith", "tinker"]


def get_vendor_keywords_for_items(item_graphics: set[int]) -> list[str]:
    """Given a set of item graphics, return vendor keywords to search for."""
    keywords: set[str] = set()
    for g in item_graphics:
        vendors = ITEM_VENDOR_MAP.get(g)
        if vendors:
            keywords.update(vendors)
    if not keywords:
        # Default fallback
        keywords = {"blacksmith", "arms"}
    return list(keywords)
