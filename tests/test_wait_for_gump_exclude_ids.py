"""wait_for_gump(exclude_ids=) waits for a genuinely NEW gump.

Regression: the recency fix (return the last-inserted key) only helps once the
new gump has arrived. When a stale/unrelated gump is already on screen, the
bare ``bool(ss.gumps)`` wait condition was already true, so ``wait_for_gump``
returned the lingering window immediately — and ``craft_via_gump`` clicked
against it (wrong serial/buttons), tripping ServUO's "Invalid gump response,
disconnecting..." path. Passing the pre-existing ids in ``exclude_ids`` makes
the wait block until a fresh gump is actually sent.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.actions.gump import wait_for_gump
from anima.perception.gump import GumpData


def _gump(gump_id: int) -> GumpData:
    return GumpData(serial=gump_id, gump_id=gump_id, x=0, y=0, layout="", text_lines=[])


def _ctx(gumps: dict[int, GumpData]) -> SimpleNamespace:
    # bus=None routes wait_for_gump through its poll fallback.
    self_state = SimpleNamespace(gumps=gumps)
    perception = SimpleNamespace(self_state=self_state)
    return SimpleNamespace(perception=perception, bus=None)


def test_excluded_stale_gump_does_not_satisfy_the_wait():
    stale = _gump(900)  # an unrelated panel already on screen
    ctx = _ctx({stale.gump_id: stale})

    # Only the stale gump exists and it is excluded -> the wait must time out
    # instead of handing the stale window straight back.
    result = asyncio.run(
        wait_for_gump(ctx, timeout=0.05, exclude_ids={900})
    )

    assert not result.success


def test_new_gump_arriving_past_a_stale_one_is_returned():
    gumps: dict[int, GumpData] = {900: _gump(900)}  # stale lingers
    ctx = _ctx(gumps)

    async def scenario() -> object:
        async def _inject() -> None:
            # The fresh craft sub-gump arrives a beat after the wait begins.
            await asyncio.sleep(0.02)
            gumps[100] = _gump(100)

        inject = asyncio.create_task(_inject())
        res = await wait_for_gump(ctx, timeout=1.0, exclude_ids={900})
        await inject
        return res

    result = asyncio.run(scenario())

    assert result.success
    # The stale id 900 is excluded; the genuinely new arrival (100) wins even
    # though it is the numerically smaller id.
    assert result.data["gump_id"] == 100
    assert result.data["gump"] is gumps[100]


def test_no_exclude_ids_is_backwards_compatible():
    only = _gump(42)
    ctx = _ctx({only.gump_id: only})

    result = asyncio.run(wait_for_gump(ctx, timeout=0.1))

    assert result.success
    assert result.data["gump_id"] == 42
    assert result.data["gump"] is only


def test_newest_unexcluded_among_several_is_returned():
    # Two fresh gumps arrived after the excluded stale one; newest wins.
    gumps = {900: _gump(900), 100: _gump(100), 200: _gump(200)}
    ctx = _ctx(gumps)

    result = asyncio.run(wait_for_gump(ctx, timeout=0.1, exclude_ids={900}))

    assert result.success
    # Insertion order: 900, 100, 200 -> last non-excluded arrival is 200.
    assert result.data["gump_id"] == 200
