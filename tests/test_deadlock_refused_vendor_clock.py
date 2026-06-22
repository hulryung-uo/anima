"""Regression: deadlock Strategy 3 prunes ``refused_vendors`` on the SAME
clock the writer uses.

``skills/trade/vendor.py`` stamps a refused vendor with ``time.monotonic()``
(``_mark_refused``) and re-checks it with ``time.monotonic()``
(``_is_refused``). The deadlock resolver's Strategy 3 prunes entries older
than 60s — but it previously compared them against ``_time.time()`` (the
wall clock it uses for ``_failed_destinations`` / ``depleted_banks``).

``time.time() - time.monotonic()`` is ~1.78e9 seconds, so a FRESH refused
vendor read ``now - ts > 60.0`` as True and was wiped on the very first
deadlock cycle, defeating the documented "don't wipe recent rejections"
behaviour and letting the agent immediately re-select the wrong-type vendor
it just got refused by.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _make_ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 100, 200, 0
    ss.hits, ss.hits_max = 100, 100
    ss.weight, ss.weight_max = 100, 400
    ss.gold = 50
    ss.equipment = {0x15: 0x101}
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.conn.connected = True
    ctx.blackboard = {}
    ctx.bus = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    return ctx


@pytest.mark.asyncio
async def test_fresh_refused_vendor_survives_deadlock_strategy3():
    """A vendor refused moments ago (fresh monotonic ts) must NOT be cleared
    by the deadlock resolver — the 300s per-vendor cooldown should keep
    holding it off."""
    planner = Planner(ProcedureRegistry())
    resolver = planner._deadlock

    ctx = _make_ctx()
    # Strategies 1 & 2 must be no-ops so we exercise Strategy 3:
    #   - no failed destinations, no depleted banks, no bank breaker.
    planner._failed_destinations.clear()
    # A vendor refused just now, stamped with the monotonic clock the
    # vendor code actually uses.
    serial = 0xABCD
    ctx.blackboard["refused_vendors"] = {serial: time.monotonic()}

    await resolver.resolve(ctx)

    # The bug wiped it (wall-vs-monotonic mismatch); the fix keeps it.
    assert serial in ctx.blackboard["refused_vendors"], (
        "a fresh refused vendor was pruned on the first deadlock cycle — "
        "Strategy 3 compared a monotonic timestamp against the wall clock"
    )


@pytest.mark.asyncio
async def test_genuinely_stale_refused_vendor_is_cleared():
    """A vendor refused well over 60s ago (by the monotonic clock) is still
    pruned — Strategy 3's escape hatch must keep working after the fix."""
    planner = Planner(ProcedureRegistry())
    resolver = planner._deadlock

    ctx = _make_ctx()
    planner._failed_destinations.clear()
    serial = 0xBEEF
    # 120s old on the monotonic clock → past the 60s Strategy-3 TTL.
    ctx.blackboard["refused_vendors"] = {serial: time.monotonic() - 120.0}

    await resolver.resolve(ctx)

    assert serial not in ctx.blackboard["refused_vendors"], (
        "a genuinely stale refused vendor should still be cleared by the "
        "deadlock escape hatch"
    )
