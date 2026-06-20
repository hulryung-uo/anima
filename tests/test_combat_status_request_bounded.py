"""_request_target_status's de-dup tracker must be bounded across a long soak.

The tracker (blackboard["combat_status_requested"]) exists only to suppress a
duplicate 0x34 status request per foe, but it is keyed by raw mobile serial and
ServUO streams an unbounded number of DISTINCT serials over a soak — every
Spawner mob the agent fights dies and despawns forever with a fresh serial.
Nothing reaped those entries, so an uncapped set leaked for the whole session.
These tests pin the FIFO-capped behaviour and confirm the de-dup contract still
holds (one request per serial; a re-engaged foe stays hot against eviction).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import (
    STATUS_REQUEST_CACHE_MAX,
    _request_target_status,
)


def _ctx():
    return SimpleNamespace(
        blackboard={},
        conn=SimpleNamespace(send_packet=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_status_request_sent_once_per_serial():
    ctx = _ctx()
    assert await _request_target_status(ctx, 0x100) is True   # first send
    assert await _request_target_status(ctx, 0x100) is False  # de-duped
    assert ctx.conn.send_packet.await_count == 1


@pytest.mark.asyncio
async def test_tracker_is_bounded_under_many_distinct_serials(monkeypatch):
    # Shrink the cap for a fast test.
    monkeypatch.setattr(cl, "STATUS_REQUEST_CACHE_MAX", 8)
    ctx = _ctx()
    for serial in range(100):  # far more distinct foes than the cap
        await _request_target_status(ctx, 0x1000 + serial)
    tracker = ctx.blackboard["combat_status_requested"]
    assert len(tracker) == 8, "the de-dup tracker must be FIFO-capped, not unbounded"


@pytest.mark.asyncio
async def test_fifo_evicts_oldest_serial(monkeypatch):
    monkeypatch.setattr(cl, "STATUS_REQUEST_CACHE_MAX", 3)
    ctx = _ctx()
    for serial in (1, 2, 3):
        await _request_target_status(ctx, serial)
    await _request_target_status(ctx, 4)  # overflow -> evicts serial 1 (oldest)
    tracker = ctx.blackboard["combat_status_requested"]
    assert set(tracker) == {2, 3, 4}


@pytest.mark.asyncio
async def test_reengaging_a_foe_keeps_it_hot_against_eviction(monkeypatch):
    monkeypatch.setattr(cl, "STATUS_REQUEST_CACHE_MAX", 3)
    ctx = _ctx()
    for serial in (1, 2, 3):
        await _request_target_status(ctx, serial)
    # Re-engage serial 1: it must become most-recent (still de-duped, no resend),
    # so the NEXT overflow evicts serial 2 (now oldest), not the re-engaged 1.
    assert await _request_target_status(ctx, 1) is False
    await _request_target_status(ctx, 4)
    tracker = ctx.blackboard["combat_status_requested"]
    assert 1 in tracker
    assert 2 not in tracker


def test_cap_default_is_generous():
    # The real cap is far above any plausible in-view foe count.
    assert STATUS_REQUEST_CACHE_MAX >= 256
