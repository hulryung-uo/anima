"""wait_for_gump returns the most recently RECEIVED gump, not the highest id.

Regression: ``ss.gumps`` is keyed by the gump TYPE id (the second 0xB0/0xDD
field). The old selection used ``max(ss.gumps.keys())``, which returns the
numerically largest type id. When an unrelated gump with a higher type id was
still on screen as a fresh gump arrived, ``wait_for_gump`` handed back the
stale gump — and ``craft_via_gump`` then clicked against its serial/buttons,
tripping ServUO's "Invalid gump response, disconnecting..." path or acting on
the wrong window. The newest arrival is the last-inserted dict key (insertion
order), which is what this asserts.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.actions.gump import wait_for_gump
from anima.perception.gump import GumpData


def _gump(gump_id: int) -> GumpData:
    return GumpData(serial=gump_id, gump_id=gump_id, x=0, y=0, layout="", text_lines=[])


def _ctx(gumps: dict[int, GumpData]) -> SimpleNamespace:
    # bus=None routes wait_for_gump through its poll fallback, which returns
    # immediately because a gump is already present.
    self_state = SimpleNamespace(gumps=gumps)
    perception = SimpleNamespace(self_state=self_state)
    return SimpleNamespace(perception=perception, bus=None)


def test_returns_last_received_even_when_an_older_gump_has_a_higher_id():
    older_high_id = _gump(900)  # arrived first, but has the largest type id
    newest_low_id = _gump(100)  # arrived last (the gump we actually care about)
    ctx = _ctx({older_high_id.gump_id: older_high_id, newest_low_id.gump_id: newest_low_id})

    result = asyncio.run(wait_for_gump(ctx, timeout=0.1))

    assert result.success
    # The old max()-based code returned 900 here; the freshest arrival is 100.
    assert result.data["gump_id"] == 100
    assert result.data["gump"] is newest_low_id


def test_returns_last_received_when_newest_has_the_higher_id():
    older_low_id = _gump(100)
    newest_high_id = _gump(900)  # incrementing craft-gump style: newest = highest
    ctx = _ctx({older_low_id.gump_id: older_low_id, newest_high_id.gump_id: newest_high_id})

    result = asyncio.run(wait_for_gump(ctx, timeout=0.1))

    assert result.success
    assert result.data["gump_id"] == 900
    assert result.data["gump"] is newest_high_id


def test_single_gump_is_returned():
    only = _gump(42)
    ctx = _ctx({only.gump_id: only})

    result = asyncio.run(wait_for_gump(ctx, timeout=0.1))

    assert result.success
    assert result.data["gump_id"] == 42
    assert result.data["gump"] is only


def test_no_gump_times_out():
    ctx = _ctx({})

    result = asyncio.run(wait_for_gump(ctx, timeout=0.05))

    assert not result.success
