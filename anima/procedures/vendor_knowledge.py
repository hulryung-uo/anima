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
