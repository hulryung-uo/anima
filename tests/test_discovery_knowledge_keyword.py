"""Discovered vendors/resources must be keyword-findable by the navigation prompt.

``LocationDiscovery.scan_and_record`` persists each discovery to the knowledge
table, and ``LocationKnowledge.build_knowledge_prompt`` surfaces them into the
LLM navigation prompt by keyword-matching the stored facts
(``query_knowledge(keyword="vendor")`` / ``keyword="resource"``).

The old fact format stored only the *subcategory* — "weaponsmith at (x, y)",
"iron_spot at (x, y)" — so the words "vendor"/"resource" never appeared in the
fact and the keyword LIKE search matched nothing: every discovered vendor and
resource was persisted yet invisible to navigation. Leading the fact with the
category keyword fixes the read/write contract.
"""

from __future__ import annotations

import pytest

from anima.memory.database import MemoryDB
from anima.navigation.discovery import DiscoveredLocation, discovery_fact
from anima.navigation.knowledge import LocationKnowledge


@pytest.fixture
async def db(tmp_path):
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


class TestDiscoveryFactKeyword:
    def test_fact_leads_with_category_keyword(self) -> None:
        vendor = DiscoveredLocation(
            category="vendor", subcategory="weaponsmith", x=1434, y=1699
        )
        resource = DiscoveredLocation(
            category="resource", subcategory="iron_spot", x=2500, y=560
        )
        # The category keyword the read path searches on must be present.
        assert discovery_fact(vendor) == "vendor: weaponsmith at (1434, 1699)"
        assert "vendor" in discovery_fact(vendor)
        assert "resource" in discovery_fact(resource)
        # And the (x, y) tail is preserved so coordinate parsing still works.
        assert "(1434, 1699)" in discovery_fact(vendor)

    @pytest.mark.asyncio
    async def test_discovered_vendor_surfaces_in_prompt(self, db: MemoryDB) -> None:
        loc = DiscoveredLocation(
            category="vendor", subcategory="weaponsmith", x=1434, y=1699
        )
        # Persist exactly as scan_and_record does.
        await db.add_knowledge(
            agent_name="anima",
            fact=discovery_fact(loc),
            source="exploration",
            confidence=0.9,
        )

        # The keyword query the navigation prompt uses must now find it.
        found = await db.query_knowledge("anima", keyword="vendor", limit=5)
        assert any("weaponsmith" in k.fact for k in found)

        kb = LocationKnowledge(memory_db=db, agent_name="anima")
        prompt = await kb.build_knowledge_prompt(1430, 1700)
        assert "Known vendors:" in prompt
        assert "weaponsmith" in prompt

    @pytest.mark.asyncio
    async def test_discovered_resource_surfaces_in_prompt(self, db: MemoryDB) -> None:
        loc = DiscoveredLocation(
            category="resource", subcategory="iron_spot", x=2500, y=560
        )
        await db.add_knowledge(
            agent_name="anima",
            fact=discovery_fact(loc),
            source="exploration",
            confidence=0.9,
        )

        kb = LocationKnowledge(memory_db=db, agent_name="anima")
        prompt = await kb.build_knowledge_prompt(2500, 560)
        assert "Known resources:" in prompt
        assert "iron_spot" in prompt
