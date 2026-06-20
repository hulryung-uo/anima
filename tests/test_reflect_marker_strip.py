"""Regression: reflect() must strip only real list markers, never digits that
are part of the fact (coordinates, counts). Uses in-memory SQLite."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.memory.database import MemoryDB
from anima.memory.learning import reflect


@pytest.fixture
async def db():
    memory = MemoryDB(":memory:")
    await memory.init()
    yield memory
    await memory.close()


def _llm(text: str) -> MagicMock:
    mock = MagicMock()
    mock.chat = AsyncMock(return_value=MagicMock(text=text))
    return mock


async def _seed_episodes(db: MemoryDB) -> None:
    for i in range(10):
        await db.record_episode(
            "Anima", 1000, 2000, "go", f"place_{i}", "success", reward=5.0,
            summary=f"Visited place_{i}",
        )


@pytest.mark.asyncio
async def test_digit_leading_facts_are_preserved(db: MemoryDB) -> None:
    """A fact whose content begins with digits (a coordinate or a count) must
    survive intact — the old lstrip-charset implementation truncated it."""
    await _seed_episodes(db)

    llm = _llm(
        "1550 is a dangerous spot to walk through\n"
        "3 trolls guard the bridge near the river"
    )

    facts = await reflect(db, llm, "Anima")

    assert "1550 is a dangerous spot to walk through" in facts
    assert "3 trolls guard the bridge near the river" in facts

    stored = {k.fact for k in await db.query_knowledge("Anima", limit=20)}
    assert "1550 is a dangerous spot to walk through" in stored
    assert "3 trolls guard the bridge near the river" in stored
    # The leading digits must NOT have been eaten.
    assert "is a dangerous spot to walk through" not in stored
    assert "trolls guard the bridge near the river" not in stored


@pytest.mark.asyncio
async def test_real_list_markers_are_stripped(db: MemoryDB) -> None:
    """Genuine leading bullets / numbering are still removed, including a
    numbered marker that precedes a coordinate fact."""
    await _seed_episodes(db)

    llm = _llm(
        "- hulryung speaks Korean and is friendly\n"
        "1. Walking near (1550, 1620) often gets blocked\n"
        "2) The bank is at (1434, 1699) in town"
    )

    facts = await reflect(db, llm, "Anima")

    assert "hulryung speaks Korean and is friendly" in facts
    # Numbering removed, but the coordinate inside the fact is intact.
    assert "Walking near (1550, 1620) often gets blocked" in facts
    assert "The bank is at (1434, 1699) in town" in facts
    # No residual marker characters leaked through.
    for f in facts:
        assert not f.startswith(("-", "*", "•"))
        assert not f[:2] in ("1.", "2)")
