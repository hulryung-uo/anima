"""Tests for Avatar.close() lifecycle cleanup.

Regression guard: the metrics background loops spawned in Avatar.create()
(`_state_diff_loop`, `_action_log_poll_loop`, `_metrics_aggregator.run_forever`)
are `while True` coroutines. Before the fix close() never cancelled them, so
every reconnect (main.py re-runs create()) leaked another set of forever-loops.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from anima.core.avatar import Avatar


def _bare_avatar() -> Avatar:
    """Construct an Avatar without going through the live create() path."""
    return Avatar(
        cfg=MagicMock(),
        conn=MagicMock(),
        perception=MagicMock(),
        walker=MagicMock(),
        pkt_handler=MagicMock(),
        persona=MagicMock(),
        bus=MagicMock(),
    )


@pytest.mark.asyncio
async def test_init_tracks_background_task_list():
    avatar = _bare_avatar()
    # The attribute must always exist so close() can iterate it even when
    # create() never ran (e.g. construction in tests / partial init).
    assert avatar._background_tasks == []


@pytest.mark.asyncio
async def test_close_cancels_background_tasks():
    avatar = _bare_avatar()
    avatar.memory_db = None  # skip db close branch

    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        while True:
            await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    avatar._background_tasks.append(task)
    await started.wait()
    assert not task.done()

    await avatar.close()

    # The leaked loop must be cancelled and the tracking list cleared so a
    # subsequent create()/close() cycle starts clean.
    assert task.cancelled()
    assert avatar._background_tasks == []


@pytest.mark.asyncio
async def test_close_is_idempotent_with_no_tasks():
    avatar = _bare_avatar()
    avatar.memory_db = None
    # No background tasks and no _log_sub — close() must not raise.
    await avatar.close()
    await avatar.close()
    assert avatar._background_tasks == []
