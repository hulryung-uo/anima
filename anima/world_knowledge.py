"""World knowledge — known locations, landmarks, and points of interest."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    name: str
    x: int
    y: int
    description: str = ""


# Britain city landmarks (Felucca/Trammel)
BRITAIN_LOCATIONS: list[Location] = [
    Location("West Britain Bank", 1434, 1699, "The famous gathering spot. Everyone comes here."),
    Location("Britain Tavern", 1610, 1591, "The Salty Dog tavern. Good place for rumors."),
    Location("Britain Blacksmith", 1416, 1757, "Forge and anvil. Weapons and armor."),
    Location("Britain Mage Shop", 1492, 1628, "Reagents and scrolls."),
    Location("Britain Healer", 1454, 1699, "Healing and resurrection."),
    Location("Britain Provisioner", 1602, 1659, "General supplies and tools."),
    Location("Britain Armorer", 1418, 1757, "Armor and shields."),
    Location("Britain Jeweler", 1464, 1600, "Gems and jewelry."),
    Location("Britain Cemetery", 1386, 1538, "Spooky place. Undead at night."),
    Location("Britain Docks", 1504, 1768, "Ships and sailors. Fishing spot."),
    Location("Britain Castle", 1323, 1624, "Lord British's castle. Grand and imposing."),
    Location("Britain Park", 1475, 1645, "Green space in the middle of town."),
    Location("Britain Stables", 1479, 1555, "Horses and pack animals."),
    Location("Bulletin Board", 1600, 1595, "Community messages and notices."),
    Location("Sweet Dreams Inn", 1585, 1590, "A cozy inn to rest."),
]

# Locations indexed by name for quick lookup
_LOCATIONS_BY_NAME: dict[str, Location] = {loc.name.lower(): loc for loc in BRITAIN_LOCATIONS}


def find_location(name: str) -> Location | None:
    """Find a location by name (case-insensitive partial match)."""
    name_lower = name.lower()
    # Exact match first
    if name_lower in _LOCATIONS_BY_NAME:
        return _LOCATIONS_BY_NAME[name_lower]
    # Partial match
    for key, loc in _LOCATIONS_BY_NAME.items():
        if name_lower in key or key in name_lower:
            return loc
    return None


def nearest_locations(x: int, y: int, count: int = 5) -> list[tuple[Location, int]]:
    """Return nearest known locations with distances."""
    results: list[tuple[Location, int]] = []
    for loc in BRITAIN_LOCATIONS:
        dist = max(abs(loc.x - x), abs(loc.y - y))  # Chebyshev distance
        results.append((loc, dist))
    results.sort(key=lambda r: r[1])
    return results[:count]


def format_locations_for_llm(x: int, y: int, count: int = 8) -> str:
    """Format nearby locations for LLM context."""
    nearby = nearest_locations(x, y, count)
    lines = ["Known places in Britain:"]
    for loc, dist in nearby:
        desc = f" — {loc.description}" if loc.description else ""
        lines.append(f"  - {loc.name} ({loc.x}, {loc.y}), ~{dist} steps away{desc}")
    return "\n".join(lines)
