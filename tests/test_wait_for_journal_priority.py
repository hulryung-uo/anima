"""wait_for_journal must resolve by PATTERN-list priority, not arrival order.

Callers pass ``patterns`` in deliberate precedence order. cast_spell's abort
scan is ``[_NO_MANA, _NO_REAGENTS, _FIZZLE, _DISRUPTED]`` and documents that the
disruption flag is last "so an explicit mana/reagent line still wins". A real
session interleaves these: being hit mid-cast emits the disruption cliloc
(500641) BEFORE the "Insufficient mana" abort, so both lines sit in the journal
within the same wait window — disruption EARLIER in time. The old scan returned
the first chronological entry matching any pattern, reporting ``disrupted``
(=> recast) when ``no_mana`` (=> stop) was the real, higher-priority reason.
These tests pin priority-by-pattern-index resolution.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.actions.journal import wait_for_journal


def _ctx(lines: list[tuple[float, str]]):
    """Build a bus=None ctx whose journal holds (timestamp, text) entries.

    bus=None routes wait_for_journal through its polling fallback, which calls
    ``_check()`` on the first pass — so a pre-seeded match resolves with no
    sleep. Timestamps are well above any real ``time.time()`` so every entry is
    considered in-window regardless of ``since``.
    """
    journal = [SimpleNamespace(timestamp=ts, text=text) for ts, text in lines]
    social = SimpleNamespace(journal=journal)
    return SimpleNamespace(
        bus=None,
        perception=SimpleNamespace(social=social),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep the polling fallback instant if a regression ever fails to match on
    # the first pass (it would otherwise just time out, but stay fast).
    import anima.actions.journal as j

    monkeypatch.setattr(j.asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
async def test_lower_pattern_index_wins_over_earlier_entry():
    # Disruption line is EARLIER in time; mana line is LATER. The patterns list
    # ranks mana (index 0) above disruption (index 3), so mana must win even
    # though it arrived second.
    patterns = [
        "Insufficient mana",          # 0 — highest priority
        "More reagents are needed",   # 1
        "fizzles",                    # 2
        "concentration is disturbed",  # 3 — lowest priority
    ]
    ctx = _ctx([
        (1e18, "Your concentration is disturbed, thus ruining thy spell."),
        (2e18, "Insufficient mana for this spell."),
    ])
    result = await wait_for_journal(ctx, patterns, timeout=1.0)
    assert result.success is True
    assert result.data["index"] == 0
    assert "Insufficient mana" in result.data["text"]


@pytest.mark.asyncio
async def test_single_match_unaffected():
    # Only one pattern present — behaviour is identical to before the fix.
    patterns = ["Insufficient mana", "fizzles", "concentration is disturbed"]
    ctx = _ctx([
        (1e18, "You feel rested."),
        (2e18, "The spell fizzles."),
    ])
    result = await wait_for_journal(ctx, patterns, timeout=1.0)
    assert result.success is True
    assert result.data["index"] == 1
    assert "fizzles" in result.data["text"]


@pytest.mark.asyncio
async def test_tie_on_pattern_index_breaks_to_earliest_entry():
    # Two entries match the SAME (only) pattern — the earliest one is returned.
    patterns = ["fizzles"]
    ctx = _ctx([
        (1e18, "The first spell fizzles out."),
        (2e18, "The second spell fizzles too."),
    ])
    result = await wait_for_journal(ctx, patterns, timeout=1.0)
    assert result.success is True
    assert result.data["index"] == 0
    assert "first spell" in result.data["text"]


@pytest.mark.asyncio
async def test_no_match_times_out_to_failure():
    patterns = ["Insufficient mana", "fizzles"]
    ctx = _ctx([(1e18, "Nothing relevant happened.")])
    result = await wait_for_journal(ctx, patterns, timeout=0.2)
    assert result.success is False


@pytest.mark.asyncio
async def test_entries_before_since_are_ignored():
    # An in-window low-priority line plus an out-of-window high-priority line:
    # the stale (pre-since) entry must not win even though it ranks higher.
    patterns = ["Insufficient mana", "fizzles"]
    ctx = _ctx([
        (10.0, "Insufficient mana for this spell."),  # before `since`
        (3e18, "The spell fizzles."),                  # in window
    ])
    result = await wait_for_journal(ctx, patterns, timeout=1.0, since=1e9)
    assert result.success is True
    assert result.data["index"] == 1
    assert "fizzles" in result.data["text"]
