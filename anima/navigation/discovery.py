"""Location discovery: automatic detection of interesting places during gameplay.

Scans the WorldState for vendor NPCs, crafting stations, banks, and resource
spots, then persists discoveries to the memory DB's knowledge table.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from anima.brain.behavior_tree import BrainContext
from anima.perception.enums import NotorietyFlag
from anima.perception.world_state import MobileInfo

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Crafting station graphic IDs (static/ground items)
# ---------------------------------------------------------------------------

FORGE_GRAPHICS: frozenset[int] = frozenset({0x0FB1, 0x197A, 0x197E, 0x19A9, 0x0DE3, 0x0DE6})
ANVIL_GRAPHICS: frozenset[int] = frozenset({0x0FAF, 0x0FB0, 0x2DD5, 0x2DD6})
SPINNING_WHEEL_GRAPHICS: frozenset[int] = frozenset({0x1015, 0x1019})
LOOM_GRAPHICS: frozenset[int] = frozenset({0x105F, 0x1060})
WATER_TROUGH_GRAPHICS: frozenset[int] = frozenset({0x0B41, 0x0B42, 0x0B43, 0x0B44})
OVEN_GRAPHICS: frozenset[int] = frozenset({0x092B, 0x0931})

# Map graphic -> (category, subcategory) for quick lookup
_CRAFTING_STATION_MAP: dict[int, tuple[str, str]] = {}
for _g in FORGE_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "forge")
for _g in ANVIL_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "anvil")
for _g in SPINNING_WHEEL_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "spinning_wheel")
for _g in LOOM_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "loom")
for _g in WATER_TROUGH_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "water_trough")
for _g in OVEN_GRAPHICS:
    _CRAFTING_STATION_MAP[_g] = ("crafting_station", "oven")

# ---------------------------------------------------------------------------
# Vendor type keywords matched against NPC name / OPL properties
# ---------------------------------------------------------------------------

VENDOR_KEYWORDS: dict[str, str] = {
    "blacksmith": "blacksmith",
    "carpenter": "carpenter",
    "tinker": "tinker",
    "healer": "healer",
    "provisioner": "provisioner",
    "mage": "mage",
    "tailor": "tailor",
    "alchemist": "alchemist",
    "herbalist": "herbalist",
    "cook": "cook",
    "baker": "baker",
    "butcher": "butcher",
    "tanner": "tanner",
    "jeweler": "jeweler",
    "scribe": "scribe",
    "weaponsmith": "weaponsmith",
    "armorer": "armorer",
    "bowyer": "bowyer",
    "fletcher": "fletcher",
    "fisherman": "fisherman",
    "innkeeper": "innkeeper",
    "barkeeper": "barkeeper",
    "bard": "bard",
    "ranger": "ranger",
    "shipwright": "shipwright",
    "veterinarian": "veterinarian",
    "architect": "architect",
    "cobbler": "cobbler",
    "furtrader": "furtrader",
    "glassblower": "glassblower",
    "mapmaker": "mapmaker",
    "weaver": "weaver",
}

# Common UO NPC human body types
_NPC_BODY_TYPES: frozenset[int] = frozenset({0x0190, 0x0191, 0x025D, 0x025E})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredLocation:
    """A location discovered through exploration."""

    category: str  # "vendor", "crafting_station", "resource", "bank"
    subcategory: str  # "carpenter", "forge", "lumber_spot", "banker"
    x: int
    y: int
    z: int = 0
    name: str = ""  # NPC name or description
    serial: int = 0  # entity serial if applicable


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------


class LocationDiscovery:
    """Scans world state to discover and record interesting locations.

    Maintains an in-memory deduplication set so each location is only
    reported once per session. Persists new discoveries to the memory DB
    knowledge table for long-term recall.
    """

    def __init__(self, agent_name: str = "anima") -> None:
        self._agent_name = agent_name
        self._seen: set[str] = set()
        self._known: list[DiscoveredLocation] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, ctx: BrainContext) -> list[DiscoveredLocation]:
        """Scan nearby world state for discoverable locations.

        Call this periodically (e.g., every brain tick or every 30s).
        Returns newly discovered locations (not previously seen).
        """
        discoveries: list[DiscoveredLocation] = []
        discoveries.extend(self._scan_vendors(ctx))
        discoveries.extend(self._scan_crafting_stations(ctx))
        discoveries.extend(self._scan_banks(ctx))
        return discoveries

    async def scan_and_record(self, ctx: BrainContext) -> list[DiscoveredLocation]:
        """Scan and persist discoveries to the memory database."""
        new_locations = self.scan(ctx)
        if new_locations and ctx.memory_db:
            for loc in new_locations:
                await ctx.memory_db.add_knowledge(
                    agent_name=self._agent_name,
                    fact=f"{loc.subcategory} at ({loc.x}, {loc.y})",
                    source="exploration",
                    confidence=0.9,
                )
        return new_locations

    def record_resource_spot(
        self, resource_type: str, x: int, y: int, z: int = 0
    ) -> DiscoveredLocation | None:
        """Record a successful resource gathering location.

        Called by skills after successful ChopWood/MineOre/etc.
        Returns the discovery if it was new, or None if already seen.
        """
        key = self._dedup_key("resource", x, y)
        if key in self._seen:
            return None
        self._seen.add(key)
        loc = DiscoveredLocation(
            category="resource",
            subcategory=f"{resource_type}_spot",
            x=x,
            y=y,
            z=z,
            name=f"{resource_type} spot",
        )
        self._known.append(loc)
        logger.info(
            "location_discovered",
            category="resource",
            subcategory=f"{resource_type}_spot",
            pos=f"({x}, {y})",
        )
        return loc

    def get_known_locations(self, category: str | None = None) -> list[DiscoveredLocation]:
        """Get all discovered locations, optionally filtered by category."""
        if category is None:
            return list(self._known)
        return [loc for loc in self._known if loc.category == category]

    # ------------------------------------------------------------------
    # Internal scanners
    # ------------------------------------------------------------------

    def _scan_vendors(self, ctx: BrainContext) -> list[DiscoveredLocation]:
        """Find vendor NPCs nearby."""
        self_state = ctx.perception.self_state
        nearby = ctx.perception.world.nearby_mobiles(self_state.x, self_state.y, distance=18)
        results: list[DiscoveredLocation] = []
        for mob in nearby:
            if not self._is_vendor_candidate(mob):
                continue
            vendor_type = self._infer_vendor_type(mob)
            if not vendor_type:
                vendor_type = "vendor"
            key = self._dedup_key("vendor", mob.x, mob.y)
            if key in self._seen:
                continue
            self._seen.add(key)
            loc = DiscoveredLocation(
                category="vendor",
                subcategory=vendor_type,
                x=mob.x,
                y=mob.y,
                z=mob.z,
                name=mob.name,
                serial=mob.serial,
            )
            results.append(loc)
            self._known.append(loc)
            logger.info(
                "location_discovered",
                category="vendor",
                subcategory=vendor_type,
                name=mob.name,
                pos=f"({mob.x}, {mob.y})",
            )
        return results

    def _scan_crafting_stations(self, ctx: BrainContext) -> list[DiscoveredLocation]:
        """Find crafting stations (forges, anvils, etc.) nearby."""
        self_state = ctx.perception.self_state
        nearby = ctx.perception.world.nearby_items(self_state.x, self_state.y, distance=18)
        results: list[DiscoveredLocation] = []
        for item in nearby:
            station = _CRAFTING_STATION_MAP.get(item.graphic)
            if station is None:
                continue
            category, subcategory = station
            key = self._dedup_key(subcategory, item.x, item.y)
            if key in self._seen:
                continue
            self._seen.add(key)
            loc = DiscoveredLocation(
                category=category,
                subcategory=subcategory,
                x=item.x,
                y=item.y,
                z=item.z,
                name=subcategory,
                serial=item.serial,
            )
            results.append(loc)
            self._known.append(loc)
            logger.info(
                "location_discovered",
                category=category,
                subcategory=subcategory,
                pos=f"({item.x}, {item.y})",
            )
        return results

    def _scan_banks(self, ctx: BrainContext) -> list[DiscoveredLocation]:
        """Find bank locations (bankers)."""
        self_state = ctx.perception.self_state
        nearby = ctx.perception.world.nearby_mobiles(self_state.x, self_state.y, distance=18)
        results: list[DiscoveredLocation] = []
        for mob in nearby:
            if not self._is_banker(mob):
                continue
            key = self._dedup_key("bank", mob.x, mob.y)
            if key in self._seen:
                continue
            self._seen.add(key)
            loc = DiscoveredLocation(
                category="bank",
                subcategory="banker",
                x=mob.x,
                y=mob.y,
                z=mob.z,
                name=mob.name,
                serial=mob.serial,
            )
            results.append(loc)
            self._known.append(loc)
            logger.info(
                "location_discovered",
                category="bank",
                subcategory="banker",
                name=mob.name,
                pos=f"({mob.x}, {mob.y})",
            )
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_vendor_candidate(mob: MobileInfo) -> bool:
        """Check if a mobile looks like a vendor NPC."""
        # Vendors are INVULNERABLE (yellow) with human body types
        if mob.notoriety != NotorietyFlag.INVULNERABLE:
            return False
        if mob.body not in _NPC_BODY_TYPES:
            return False
        return True

    @staticmethod
    def _infer_vendor_type(mob: MobileInfo) -> str:
        """Try to determine the vendor specialisation from name/properties."""
        # Combine name and all OPL property lines into a single search string
        search_text = mob.name.lower()
        for prop in mob.properties:
            search_text += " " + prop.lower()

        for keyword, vendor_type in VENDOR_KEYWORDS.items():
            if keyword in search_text:
                return vendor_type
        return ""

    @staticmethod
    def _is_banker(mob: MobileInfo) -> bool:
        """Check if a mobile is a banker NPC."""
        if mob.notoriety != NotorietyFlag.INVULNERABLE:
            return False
        if mob.body not in _NPC_BODY_TYPES:
            return False
        search_text = mob.name.lower()
        for prop in mob.properties:
            search_text += " " + prop.lower()
        return "banker" in search_text

    @staticmethod
    def _dedup_key(category: str, x: int, y: int, radius: int = 5) -> str:
        """Generate deduplication key rounding position to a grid."""
        rx = x // radius * radius
        ry = y // radius * radius
        return f"{category}:{rx}:{ry}"
