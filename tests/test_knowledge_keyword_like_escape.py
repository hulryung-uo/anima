"""query_knowledge must treat the keyword as a literal substring.

The keyword reaches query_knowledge straight from discovery/navigation
(``find_resource_area`` / ``find_crafting_station`` fall back to the raw
station/resource string), and those strings routinely contain SQLite LIKE
metacharacters: '_' (e.g. ``spinning_wheel``, ``night_sight``) and, more
rarely, '%'. Before the fix the keyword was interpolated straight into a
``LIKE '%...%'`` pattern, so '_' matched ANY single character and '%' matched
anything — the search silently broadened and ``_search_memory`` could pull
coordinates out of a completely unrelated fact, sending the agent to the wrong
location. These tests pin the literal-substring contract using in-memory
SQLite.
"""

from __future__ import annotations

import pytest

from anima.memory.database import MemoryDB


@pytest.fixture
async def db():
    memory = MemoryDB(":memory:")  # in-memory SQLite
    await memory.init()
    yield memory
    await memory.close()


class TestKnowledgeKeywordLikeEscape:
    @pytest.mark.asyncio
    async def test_underscore_is_literal_not_wildcard(self, db: MemoryDB) -> None:
        # A fact whose phrasing differs from the keyword only where the '_' sits.
        # If '_' were a LIKE wildcard it would match "spinning wheel" too and the
        # query would return a fact the agent never recorded under that key.
        await db.add_knowledge("Anima", "The spinning wheel is at (1000, 2000)")

        found = await db.query_knowledge("Anima", keyword="spinning_wheel", limit=5)
        # 'spinning_wheel' is NOT a substring of 'spinning wheel' once '_' is
        # treated literally, so nothing should match.
        assert found == []

    @pytest.mark.asyncio
    async def test_underscore_keyword_still_finds_exact_fact(self, db: MemoryDB) -> None:
        # The legitimate case must keep working: a fact stored with the literal
        # underscore is found by the underscore keyword.
        await db.add_knowledge("Anima", "station spinning_wheel at (1500, 2500)")
        await db.add_knowledge("Anima", "the forge sits at (10, 20)")

        found = await db.query_knowledge("Anima", keyword="spinning_wheel", limit=5)
        assert len(found) == 1
        assert "spinning_wheel" in found[0].fact

    @pytest.mark.asyncio
    async def test_percent_is_literal_not_match_all(self, db: MemoryDB) -> None:
        # A bare '%' keyword previously matched EVERY fact for the agent. It must
        # now match only facts that literally contain a percent sign.
        await db.add_knowledge("Anima", "the bank is at (1434, 1699)")
        await db.add_knowledge("Anima", "ore yield is 50% near (1550, 1620)")

        found = await db.query_knowledge("Anima", keyword="%", limit=10)
        assert len(found) == 1
        assert "50%" in found[0].fact

    @pytest.mark.asyncio
    async def test_plain_keyword_unaffected(self, db: MemoryDB) -> None:
        # Regression guard: ordinary keywords with no metacharacters keep their
        # substring-match behaviour and confidence ordering.
        await db.add_knowledge("Anima", "the bank is at (1434, 1699)", confidence=0.4)
        await db.add_knowledge("Anima", "another bank entrance (1440, 1705)", confidence=0.9)

        found = await db.query_knowledge("Anima", keyword="bank", limit=5)
        assert len(found) == 2
        # Still ordered by confidence DESC.
        assert found[0].confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_backslash_keyword_is_literal(self, db: MemoryDB) -> None:
        # The ESCAPE char itself ('\') must be escaped so a keyword containing a
        # backslash matches literally rather than corrupting the pattern.
        await db.add_knowledge("Anima", r"path C:\mine is rich at (900, 800)")

        found = await db.query_knowledge("Anima", keyword=r"C:\mine", limit=5)
        assert len(found) == 1
        assert r"C:\mine" in found[0].fact
