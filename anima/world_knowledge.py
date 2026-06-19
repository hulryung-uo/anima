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
    # --- Banks --- (from ServUO Common.map)
    Location("West Britain Bank", 1425, 1690, "The First Bank of Britain."),
    Location("East Britain Bank", 1655, 1606, "East Britain Bank."),
    # --- Shops ---
    Location("Britain Tinker", 1422, 1654, "Tinker's Guild. Tools, pickaxes, shovels."),
    Location("Britain Carpenter", 1430, 1597, "The Saw Horse."),
    Location("Britain Blacksmith", 1365, 1575, "The Lord's Arms. Forge and anvil."),
    Location("Britain Provisioner", 1469, 1668, "Britain's Premier Provisioners."),
    Location("Britain Provisioner East", 1602, 1712, "Profuse Provisions."),
    Location("Britain Armorer", 1637, 1693, "Artistic Armour."),
    Location("Britain Arms", 1443, 1650, "Heavy Metal Armorer."),
    Location("Britain Arms North", 1481, 1584, "Strength and Steel."),
    Location("Britain Healer", 1472, 1607, "Healer of Britain."),
    Location("Britain Mage Shop", 1590, 1654, "Incantations & Enchantments."),
    Location("Britain Reagents", 1498, 1659, "Ethereal Goods."),
    Location("Britain Jeweler", 1655, 1642, "A Girl's Best Friend."),
    Location("Britain Jeweler West", 1451, 1679, "Premier Gems."),
    Location("Britain Tailor", 1467, 1686, "The Lord's Clothiers."),
    Location("Britain Tailor East", 1547, 1659, "The Right Fit."),
    Location("Britain Bowyer", 1470, 1578, "Quality Fletching."),
    Location("Britain Baker", 1450, 1617, "Good Eats."),
    Location("Britain Tanner", 1431, 1612, "The Best Hides of Britain."),
    Location("Britain Butcher", 1449, 1723, "The Cleaver."),
    # --- Landmarks ---
    Location("Britain Tavern", 1620, 1586, "The Salty Dog."),
    Location("Britain Blue Boar", 1498, 1691, "The Blue Boar tavern."),
    Location("Britain Cat's Lair", 1427, 1716, "The Cat's Lair tavern."),
    Location("Sweet Dreams Inn", 1493, 1619, "Sweet Dreams inn."),
    Location("Wayfarer's Inn", 1585, 1591, "The Wayfarer's Inn."),
    Location("Britain Stables", 1385, 1658, "The Stables."),
    Location("Britain Docks", 1504, 1768, "Ships and sailors. Fishing spot."),
    # --- Forests ---
    Location("Britain North Forest", 1620, 1554, "Dense forest north of town. Good for lumber."),
    Location("Britain East Forest", 1640, 1551, "Oak and walnut trees east of town."),
]

# Minoc city landmarks (Felucca) — mining hub
# Coordinates from ServUO spawn data and in-game exploration.
MINOC_LOCATIONS: list[Location] = [
    # --- Bank ---
    Location("Minoc Bank", 2503, 552, "Bank of Minoc."),
    # --- Shops (from ServUO Common.map) ---
    Location("Minoc Blacksmith", 2471, 564, "The Forgery. Forge and anvil. Buys ingots/weapons."),
    Location("Minoc Tinker", 2461, 457, "Gears and Gadgets. Sells pickaxes, shovels, tinker tools."),
    Location("Minoc Provisioner", 2456, 428, "The Old Miners' Supplies."),
    Location("Minoc Provisioner South", 2530, 551, "The Survival Shop."),
    Location("Minoc Healer", 2577, 599, "The Healing Hand."),
    Location("Minoc Carpenter", 2513, 477, "The Oak Throne."),
    Location("Minoc Arms", 2533, 576, "Warrior's Battle Gear."),
    Location("Minoc Tanner", 2524, 524, "The Stretched Hide."),
    Location("Minoc Butcher", 2438, 410, "The Slaughtered Cow."),
    Location("Minoc Bard", 2424, 555, "The Mystical Lute."),
    # --- Guilds ---
    Location("Minoc Miners Guild North", 2505, 432, "The Golden Pick Axe."),
    Location("Minoc Miners Guild South", 2455, 487, "The Matewan."),
    Location("Minoc Warriors Guild", 2482, 428, "The New World Order."),
    Location("Minoc Counselor Guild", 2434, 438, "Counselor's Guild."),
    # --- Landmarks ---
    Location("Minoc Tavern", 2475, 397, "The Barnacle inn."),
    Location("Minoc Stables", 2526, 375, "Stables."),
    Location("Minoc Town Hall", 2429, 528, "Minoc Town Hall."),
    Location("Minoc Statues", 2465, 522, "Town center statues."),
    # --- Forge (near mine) ---
    Location("Minoc Forge", 2569, 482, "Forge near the mine entrance."),
    # --- Mining areas ---
    Location("Minoc East Mine", 2553, 496, "The East Mines. Rich iron veins."),
    Location("Minoc Mining Camp", 2590, 532, "Mining camp east of town."),
    # --- Waypoints ---
    Location("Minoc Road North", 2475, 415, "Main road north of town center."),
]

# All known locations across cities
ALL_LOCATIONS: list[Location] = BRITAIN_LOCATIONS + MINOC_LOCATIONS

# Locations indexed by name for quick lookup
_LOCATIONS_BY_NAME: dict[str, Location] = {loc.name.lower(): loc for loc in ALL_LOCATIONS}


def find_location(name: str, near: tuple[int, int] | None = None) -> Location | None:
    """Find a location by name (case-insensitive partial match)."""
    # An empty / whitespace-only name must NOT match anything. The partial-match
    # loop below tests ``name_lower in key`` and ``"" in key`` is *always* True,
    # so a blank name would silently resolve to the first location in the list
    # (West Britain Bank). LLM ``go`` decisions routinely arrive with the place
    # missing/null/blank (``{"action": "go", "place": ""}``); think.llm_think
    # reads ``action.get("place", "")`` and hands that straight here. Treat a
    # blank name as "unknown place" so the caller takes its goal_place_unknown
    # branch instead of committing to a bogus destination.
    name_lower = name.strip().lower()
    if not name_lower:
        return None
    # Exact match first
    if name_lower in _LOCATIONS_BY_NAME:
        return _LOCATIONS_BY_NAME[name_lower]
    # Partial match. Many keys share a discriminating prefix only ("Britain
    # Provisioner" vs "Minoc Provisioner", "...Guild North" vs "...Guild
    # South"), so an abbreviated LLM place name ("provisioner", "bank",
    # "blacksmith") matches several locations. Returning the *first* in dict
    # (== list) order silently sent the agent to whichever city happens to be
    # declared first in ALL_LOCATIONS (Britain), even when it is standing in
    # Minoc and was shown only Minoc places — abandoning its whole region to
    # walk thousands of tiles to the wrong shop. Collect every partial
    # candidate and, when the caller supplies the agent's position, prefer the
    # geographically nearest (Chebyshev to nav point — the tile actually walked
    # to). With no position the legacy first-match order is preserved.
    candidates = [
        loc
        for key, loc in _LOCATIONS_BY_NAME.items()
        if name_lower in key or key in name_lower
    ]
    if not candidates:
        return None
    if near is not None:
        ax, ay = near
        candidates.sort(key=lambda loc: max(abs(loc.nav_x - ax), abs(loc.nav_y - ay)))
    return candidates[0]


def nearest_locations(x: int, y: int, count: int = 5) -> list[tuple[Location, int]]:
    """Return nearest known locations with distances.

    Distance is measured to the navigable point (``nav_x``/``nav_y``) — the
    spot the agent actually walks to — not the raw (possibly indoor) coords.
    This matches every navigation consumer (planner/roaming/movement/think).
    """
    results: list[tuple[Location, int]] = []
    for loc in ALL_LOCATIONS:
        dist = max(abs(loc.nav_x - x), abs(loc.nav_y - y))  # Chebyshev distance
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
