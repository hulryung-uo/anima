"""World knowledge — known locations, landmarks, and points of interest."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    name: str
    x: int
    y: int
    description: str = ""
    # Outdoor approach point — if set, agent navigates here instead of (x, y).
    # Use for indoor locations where the exact coords are inside a building.
    approach_x: int | None = None
    approach_y: int | None = None

    @property
    def nav_x(self) -> int:
        """X coordinate to navigate to (approach point or exact)."""
        return self.approach_x if self.approach_x is not None else self.x

    @property
    def nav_y(self) -> int:
        """Y coordinate to navigate to (approach point or exact)."""
        return self.approach_y if self.approach_y is not None else self.y


# Britain city landmarks (Felucca/Trammel)
# Coordinates from ServUO trammel.xml spawn data.
# Locations with approach_x/y are indoor — agent stops at the outdoor approach point.
BRITAIN_LOCATIONS: list[Location] = [
    # --- Banks ---
    Location("West Britain Bank", 1427, 1683, "The famous gathering spot. Banker inside.",
             approach_x=1438, approach_y=1693),
    # --- Shops (from ServUO spawns) ---
    Location("Britain Carpenter", 1430, 1597, "Carpentry shop. Sells saws and wooden tools.",
             approach_x=1435, approach_y=1600),
    Location("Britain Tinker", 1425, 1655, "Tinker shop. Sells tinker tools, saws, pickaxes.",
             approach_x=1427, approach_y=1658),
    Location("Britain Blacksmith", 1418, 1547, "Forge and anvil. Weapons and armor.",
             approach_x=1418, approach_y=1550),
    Location("Britain Provisioner", 1470, 1664, "General supplies and tools."),
    Location("Britain Armorer", 1447, 1647, "Armor and shields.",
             approach_x=1447, approach_y=1650),
    Location("Britain Healer", 1471, 1611, "Healing and resurrection.",
             approach_x=1471, approach_y=1614),
    Location("Britain Mage Shop", 1484, 1545, "Reagents and scrolls.",
             approach_x=1484, approach_y=1548),
    Location("Britain Jeweler", 1451, 1679, "Gems and jewelry."),
    Location("Britain Tailor", 1467, 1686, "Cloth, thread, and tailored goods."),
    Location("Britain Bowyer", 1470, 1578, "Bows and arrows.",
             approach_x=1470, approach_y=1581),
    Location("Britain Baker", 1450, 1617, "Fresh bread and food.",
             approach_x=1450, approach_y=1620),
    Location("Britain Tanner", 1427, 1609, "Leather and hides.",
             approach_x=1427, approach_y=1612),
    Location("Britain Butcher", 1449, 1723, "Meat and raw food."),
    # --- Landmarks ---
    Location("Britain Tavern", 1620, 1585, "The Salty Dog tavern. Good place for rumors.",
             approach_x=1620, approach_y=1588),
    Location("Sweet Dreams Inn", 1584, 1591, "A cozy inn to rest.",
             approach_x=1584, approach_y=1594),
    Location("Britain Castle", 1323, 1624, "Lord British's castle. Grand and imposing."),
    Location("Britain Park", 1475, 1645, "Green space in the middle of town."),
    Location("Britain Stables", 1479, 1555, "Horses and pack animals."),
    Location("Britain Cemetery", 1386, 1538, "Spooky place. Undead at night."),
    Location("Britain Docks", 1504, 1768, "Ships and sailors. Fishing spot."),
    # --- Forests ---
    Location("Britain North Forest", 1620, 1554, "Dense forest north of town. Good for lumber."),
    Location("Britain East Forest", 1640, 1551, "Oak and walnut trees east of town."),
]

# Minoc city landmarks (Felucca) — mining hub
# Coordinates from ServUO spawn data and in-game exploration.
MINOC_LOCATIONS: list[Location] = [
    # --- Banks ---
    Location("Minoc Bank", 2498, 400, "Minoc bank. Store ingots and gold.",
             approach_x=2499, approach_y=404),
    # --- Shops ---
    Location("Minoc Blacksmith", 2450, 408, "Forge and anvil. Buy hammers and armor.",
             approach_x=2453, approach_y=411),
    Location("Minoc Tinker", 2479, 416, "Tinker tools, pickaxes, and shovels.",
             approach_x=2480, approach_y=419),
    Location("Minoc Provisioner", 2509, 421, "General supplies."),
    Location("Minoc Healer", 2466, 395, "Healing and resurrection."),
    # --- Landmarks ---
    Location("Minoc Inn", 2476, 413, "The Barnacle inn. Rest and resupply.",
             approach_x=2476, approach_y=416),
    Location("Minoc Guildmaster", 2455, 395, "Mining guild hall."),
    # --- Mining areas ---
    Location("Minoc East Mine", 2556, 468, "Eastern mine entrance. Rich iron veins."),
    Location("Minoc North Mine", 2514, 332, "Northern caves. Deep tunnels."),
    Location("Minoc Mountain", 2560, 400, "Mountain area east of town. Open-pit mining."),
]

# All known locations across cities
ALL_LOCATIONS: list[Location] = BRITAIN_LOCATIONS + MINOC_LOCATIONS

# Locations indexed by name for quick lookup
_LOCATIONS_BY_NAME: dict[str, Location] = {loc.name.lower(): loc for loc in ALL_LOCATIONS}


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
    for loc in ALL_LOCATIONS:
        dist = max(abs(loc.x - x), abs(loc.y - y))  # Chebyshev distance
        results.append((loc, dist))
    results.sort(key=lambda r: r[1])
    return results[:count]


def format_locations_for_llm(x: int, y: int, count: int = 8) -> str:
    """Format nearby locations for LLM context."""
    nearby = nearest_locations(x, y, count)
    lines = ["Known places:"]
    for loc, dist in nearby:
        desc = f" — {loc.description}" if loc.description else ""
        lines.append(f"  - {loc.name} ({loc.x}, {loc.y}), ~{dist} steps away{desc}")
    return "\n".join(lines)
