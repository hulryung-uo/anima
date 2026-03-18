"""Unified location knowledge — combines static, discovered, and forum sources."""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from anima.memory.database import MemoryDB
from anima.skills.forum import ForumClient, ForumPost
from anima.world_knowledge import (
    BRITAIN_LOCATIONS,
    Location,
    find_location,
    nearest_locations,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Coordinate regex used to parse forum posts
# ---------------------------------------------------------------------------

# Matches "(1234, 5678)" or "( 1234, 5678 )" etc.
_COORD_PARENS_RE = re.compile(r"\(\s*(\d{1,5})\s*,\s*(\d{1,5})\s*\)")
# Matches "x=1234 y=5678" or "x=1234, y=5678"
_COORD_EQUALS_RE = re.compile(r"x\s*=\s*(\d{1,5})\s*[,\s]\s*y\s*=\s*(\d{1,5})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LocationResult:
    """A location result from any knowledge source."""

    name: str  # "Britain Carpenter Vendor"
    x: int
    y: int
    z: int = 0
    source: str = ""  # "base_knowledge", "discovery", "forum"
    confidence: float = 1.0
    details: str = ""  # additional info


# ---------------------------------------------------------------------------
# LocationKnowledge
# ---------------------------------------------------------------------------


class LocationKnowledge:
    """Unified location knowledge combining static, discovered, and shared knowledge.

    Query order:
    1. Discovery (personal experience, highest confidence)
    2. Base knowledge (common knowledge, always available)
    3. Forum (shared by other agents, variable confidence)
    """

    def __init__(
        self,
        memory_db: MemoryDB | None = None,
        forum_client: ForumClient | None = None,
        agent_name: str = "anima",
    ) -> None:
        self._memory_db = memory_db
        self._forum_client = forum_client
        self._agent_name = agent_name

    # -------------------------------------------------------------------
    # Public query methods
    # -------------------------------------------------------------------

    async def find_vendor(
        self,
        vendor_type: str,
        near_x: int = 0,
        near_y: int = 0,
    ) -> LocationResult | None:
        """Find a vendor of the given type.

        vendor_type: "carpenter", "blacksmith", "tinker", "healer", etc.
        Returns nearest known vendor location, or city center with that feature.
        """
        vt = vendor_type.lower()

        # 1. Check discovered vendors from memory DB
        result = await self._search_memory(vt, near_x, near_y)
        if result:
            return result

        # 2. Check base knowledge: location names containing the vendor type
        result = self._search_static(vt, near_x, near_y)
        if result:
            return result

        # 3. Check forum posts
        result = await self._search_forum(vt, near_x, near_y)
        if result:
            return result

        return None

    async def find_crafting_station(
        self,
        station_type: str,
        near_x: int = 0,
        near_y: int = 0,
    ) -> LocationResult | None:
        """Find a crafting station (forge, anvil, loom, etc.)."""
        st = station_type.lower()

        # Map station types to likely vendor/location keywords
        station_keywords = {
            "forge": ["blacksmith", "forge", "smithy"],
            "anvil": ["blacksmith", "anvil", "smithy"],
            "loom": ["tailor", "weaver", "loom"],
            "spinning_wheel": ["tailor", "spinning"],
            "oven": ["baker", "cook", "oven"],
        }
        keywords = station_keywords.get(st, [st])

        # 1. Check discovered locations from memory
        for kw in keywords:
            result = await self._search_memory(kw, near_x, near_y)
            if result:
                return result

        # 2. Check base knowledge
        for kw in keywords:
            result = self._search_static(kw, near_x, near_y)
            if result:
                return result

        # 3. Check forum
        for kw in keywords:
            result = await self._search_forum(kw, near_x, near_y)
            if result:
                return result

        return None

    async def find_resource_area(
        self,
        resource_type: str,
        near_x: int = 0,
        near_y: int = 0,
    ) -> LocationResult | None:
        """Find a known resource area (lumber, mining, fishing)."""
        rt = resource_type.lower()

        # Map resource types to search keywords
        resource_keywords = {
            "lumber": ["lumber", "trees", "forest", "wood"],
            "mining": ["mining", "cave", "mountain", "ore"],
            "fishing": ["fishing", "docks", "water", "pier"],
            "wood": ["lumber", "trees", "forest", "wood"],
            "ore": ["mining", "cave", "mountain", "ore"],
            "fish": ["fishing", "docks", "water", "pier"],
        }
        keywords = resource_keywords.get(rt, [rt])

        # 1. Check discovered resource spots from memory
        for kw in keywords:
            result = await self._search_memory(kw, near_x, near_y)
            if result:
                return result

        # 2. Check base knowledge resource hints
        for kw in keywords:
            result = self._search_static(kw, near_x, near_y)
            if result:
                return result

        # 3. Check forum posts
        for kw in keywords:
            result = await self._search_forum(kw, near_x, near_y)
            if result:
                return result

        return None

    async def find_bank(
        self,
        near_x: int = 0,
        near_y: int = 0,
    ) -> LocationResult | None:
        """Find the nearest known bank."""
        # 1. Check discovered banks from memory
        result = await self._search_memory("bank", near_x, near_y)
        if result:
            return result

        # 2. Check base knowledge
        result = self._search_static("bank", near_x, near_y)
        if result:
            return result

        # 3. Check forum
        result = await self._search_forum("bank", near_x, near_y)
        if result:
            return result

        return None

    # -------------------------------------------------------------------
    # Sharing / learning from forum
    # -------------------------------------------------------------------

    async def share_discovery(
        self,
        location: LocationResult,
        category: str = "exploration",
    ) -> None:
        """Share a discovery on the forum for other agents.

        Posts to the forum with the discovery details.
        Only shares significant discoveries (vendors, new areas).
        """
        if self._forum_client is None:
            return

        post_text = (
            f"[{category}] {location.name} at ({location.x}, {location.y}). {location.details}"
        )
        await self._forum_client.create_post(
            title=f"Discovered: {location.name}",
            body=post_text,
            category=category,
        )

    async def learn_from_forum(self, topic: str = "") -> list[LocationResult]:
        """Read forum posts to learn about locations other agents discovered.

        Parses posts in the 'exploration' category for location data.
        """
        if self._forum_client is None:
            return []

        posts = await self._forum_client.read_posts(category="exploration", limit=20)
        results: list[LocationResult] = []
        for post in posts:
            if topic and topic.lower() not in post.body.lower():
                continue
            parsed = self._parse_location_from_post(post)
            if parsed:
                results.append(parsed)
        return results

    # -------------------------------------------------------------------
    # LLM prompt builder
    # -------------------------------------------------------------------

    async def build_knowledge_prompt(self, x: int, y: int) -> str:
        """Build a knowledge summary for LLM prompts.

        Returns a text block describing what the agent knows about
        the current area -- nearby cities, known vendors, resources.
        Injected into LLM thinking prompts.
        """
        lines: list[str] = []

        # Current area from static knowledge
        nearby = nearest_locations(x, y, count=1)
        if nearby:
            loc, dist = nearby[0]
            if dist <= 50:
                lines.append(f"Current location: {loc.name} -- {loc.description}")
            else:
                lines.append(f"Nearest city: {loc.name} ({dist} tiles away)")

        # Known vendors nearby from discovery
        if self._memory_db is not None:
            vendor_facts = await self._memory_db.query_knowledge(
                self._agent_name, keyword="vendor", limit=5
            )
            if vendor_facts:
                vendor_lines = [f"  - {k.fact}" for k in vendor_facts]
                lines.append("Known vendors:\n" + "\n".join(vendor_lines))

            # Known resources nearby
            resource_facts = await self._memory_db.query_knowledge(
                self._agent_name, keyword="resource", limit=5
            )
            if resource_facts:
                res_lines = [f"  - {k.fact}" for k in resource_facts]
                lines.append("Known resources:\n" + "\n".join(res_lines))

        # Recent forum intelligence
        if self._forum_client is not None:
            try:
                forum_locations = await self.learn_from_forum()
                if forum_locations:
                    forum_lines = [
                        f"  - {fl.name} at ({fl.x}, {fl.y})" for fl in forum_locations[:5]
                    ]
                    lines.append("Recent discoveries (forum):\n" + "\n".join(forum_lines))
            except Exception:
                logger.debug("forum_knowledge_fetch_failed", exc_info=True)

        return "\n".join(lines)

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    async def _search_memory(self, keyword: str, near_x: int, near_y: int) -> LocationResult | None:
        """Search the memory DB knowledge table for location facts."""
        if self._memory_db is None:
            return None

        facts = await self._memory_db.query_knowledge(self._agent_name, keyword=keyword, limit=10)
        if not facts:
            return None

        # Try to parse coordinates from facts
        best: LocationResult | None = None
        best_dist = float("inf")
        for fact in facts:
            match = _COORD_PARENS_RE.search(fact.fact)
            if not match:
                match = _COORD_EQUALS_RE.search(fact.fact)
            if match:
                fx, fy = int(match.group(1)), int(match.group(2))
                dist = abs(fx - near_x) + abs(fy - near_y)
                if dist < best_dist:
                    best_dist = dist
                    best = LocationResult(
                        name=fact.fact[:80],
                        x=fx,
                        y=fy,
                        source="discovery",
                        confidence=fact.confidence,
                        details=fact.fact,
                    )

        return best

    def _search_static(self, keyword: str, near_x: int, near_y: int) -> LocationResult | None:
        """Search static world knowledge for a keyword match."""
        kw = keyword.lower()
        matches: list[tuple[Location, int]] = []

        for loc in BRITAIN_LOCATIONS:
            if kw in loc.name.lower() or kw in loc.description.lower():
                dist = abs(loc.x - near_x) + abs(loc.y - near_y)
                matches.append((loc, dist))

        if not matches:
            # Also try the find_location helper for partial matching
            loc = find_location(keyword)
            if loc:
                dist = abs(loc.x - near_x) + abs(loc.y - near_y)
                matches.append((loc, dist))

        if not matches:
            return None

        # Return nearest match
        matches.sort(key=lambda m: m[1])
        loc, _dist = matches[0]
        return LocationResult(
            name=loc.name,
            x=loc.x,
            y=loc.y,
            source="base_knowledge",
            confidence=1.0,
            details=loc.description,
        )

    async def _search_forum(self, keyword: str, near_x: int, near_y: int) -> LocationResult | None:
        """Search forum posts for location mentions."""
        if self._forum_client is None:
            return None

        try:
            posts = await self._forum_client.search(keyword)
        except Exception:
            logger.debug("forum_search_failed", keyword=keyword, exc_info=True)
            return None

        if not posts:
            return None

        best: LocationResult | None = None
        best_dist = float("inf")
        for post in posts:
            parsed = self._parse_location_from_post(post)
            if parsed:
                dist = abs(parsed.x - near_x) + abs(parsed.y - near_y)
                if dist < best_dist:
                    best_dist = dist
                    best = parsed

        return best

    @staticmethod
    def _parse_location_from_post(post: ForumPost) -> LocationResult | None:
        """Extract location data from a forum post."""
        text = post.body

        # Try parenthesized coordinates first: (1234, 5678)
        match = _COORD_PARENS_RE.search(text)
        if not match:
            # Try x=1234 y=5678 form
            match = _COORD_EQUALS_RE.search(text)
        if not match:
            return None

        x, y = int(match.group(1)), int(match.group(2))

        # Use the post title as the name, falling back to first 60 chars of body
        name = post.title or text[:60]

        return LocationResult(
            name=name,
            x=x,
            y=y,
            source="forum",
            confidence=0.6,
            details=text,
        )
