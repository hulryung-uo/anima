"""wait_for_target must not phantom-succeed when the cursor vanishes mid-wait.

Regression: ``wait_for_target`` selected the cursor fields with

    pt = ss.pending_target or {}
    cursor_id = pt.get("cursor_id", 0)

``ok`` reflects the predicate at the instant the bus wait resolved, but
``ss.pending_target`` is mutated by packet handlers running concurrently on the
same event loop. A server WITHDRAW cursor (0x6C with ``cursor_flag == 3``) and
the living->ghost death transition both null ``pending_target``. One landing in
the window between the wait waking and ``wait_for_target`` resuming left
``pending_target`` None at read time. ``None or {}`` collapsed to an empty dict,
so ``cursor_id`` defaulted to 0 and the function returned ``success=True`` with
a ZERO cursor id — every caller (cast_spell / use_on_object / use_on_target /
use_skill_on) then fired a target response against a cursor the server had
already retired, a silent no-op the action layer reported as a SUCCESS.

The fix degrades to the normal "Target cursor timeout" failure that callers
already handle, mirroring wait_for_gump's TOCTOU guard.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.actions.target import wait_for_target


def test_cursor_withdrawn_between_wait_resolving_and_read_is_timeout():
    """Bus wait reports satisfied, but a concurrent handler nulled the cursor.

    Models the TOCTOU directly: ``wait_for_condition`` returns True (it observed
    a cursor at some instant), then a server WITHDRAW (flag 3) / death handler
    set ``pending_target = None`` before ``wait_for_target`` resumed. The result
    MUST be a graceful failure, never a success carrying cursor_id == 0.
    """
    ss = SimpleNamespace(pending_target={"cursor_id": 0xABCD, "target_type": 0, "cursor_flag": 1})

    class _RacingBus:
        async def wait_for_condition(self, predicate, timeout=5.0):  # noqa: ARG002
            # A concurrent 0x6C-withdraw / ghost-transition handler nulls the
            # cursor in the window between the wait resolving and the resume.
            ss.pending_target = None
            return True

    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss), bus=_RacingBus())

    result = asyncio.run(wait_for_target(ctx, timeout=0.1))
    # On HEAD this returned success=True with data["cursor_id"] == 0.
    assert not result.success
    assert "timeout" in result.message.lower()


def test_live_cursor_still_resolves_with_real_fields():
    """A genuinely-present cursor still resolves and surfaces its real fields."""
    pending = {"cursor_id": 0x1234, "target_type": 1, "cursor_flag": 2}
    ss = SimpleNamespace(pending_target=dict(pending))

    class _Bus:
        async def wait_for_condition(self, predicate, timeout=5.0):  # noqa: ARG002
            return True

    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss), bus=_Bus())

    result = asyncio.run(wait_for_target(ctx, timeout=0.1))
    assert result.success
    assert result.data["cursor_id"] == 0x1234
    assert result.data["target_type"] == 1
    assert result.data["cursor_flag"] == 2
    # Consumed so it can't satisfy the next wait.
    assert ss.pending_target is None
