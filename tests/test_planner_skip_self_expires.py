"""A repeat-failure skip must self-expire so a busy agent recovers it.

Asymmetric-latch regression (the generic counterpart of mine_ore's, see
test_planner_mine_skip_self_clears.py): when a NON-mining procedure fails
``_REPEAT_FAIL_LIMIT`` times in a row, ``_check_stuck`` adds it to the
``_skip_procedures`` set so ``_get_proc`` returns None for it.

That set is cleared ONLY by the deadlock resolver, which fires solely when the
planner goes fully IDLE. An agent that keeps busy with other work (mining /
smelting after, say, ``sell_to_vendor`` latched out) never goes idle, so the
resolver never runs and the procedure stays skipped FOREVER — even after the
condition that caused the failures (a vendor that walked off, a station out of
range) has long cleared.

The fix gives every skip a ``_SKIP_PROC_TTL_S`` self-expiry, pruned at the top
of each tick via ``_prune_expired_skips``. These tests lock that in:
  - a fresh skip survives (still within its TTL),
  - an aged skip is dropped (re-armed for retry),
  - the parallel ``_skip_procedures_at`` stamp dict never outlives the set.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


def _make_planner() -> Planner:
    return Planner(ProcedureRegistry())


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=SimpleNamespace(x=10, y=20, gold=0)),
        blackboard={},
        bus=None,
    )


async def _latch_out(planner: Planner, ctx: SimpleNamespace, proc: str) -> None:
    """Drive a procedure into the permanent skip set via a failure streak."""
    planner._idle_ticks = planner._IDLE_WARN  # repeat scan runs above this rung
    planner._repeat_counter[proc] = planner._REPEAT_FAIL_LIMIT
    await planner._check_stuck(ctx)


@pytest.mark.asyncio
async def test_skip_self_expires_after_ttl_without_deadlock_resolver():
    """An aged skip is pruned by _prune_expired_skips even though the planner
    never goes idle (so the deadlock resolver never fires)."""
    import time as _time

    planner = _make_planner()
    ctx = _ctx()

    await _latch_out(planner, ctx, "sell_to_vendor")
    assert "sell_to_vendor" in ctx.blackboard["_skip_procedures"]

    # Backdate the stamp past the TTL — the failure cause has long cleared.
    ctx.blackboard["_skip_procedures_at"]["sell_to_vendor"] = (
        _time.time() - planner._SKIP_PROC_TTL_S - 1.0
    )

    dropped = planner._prune_expired_skips(ctx)

    assert dropped == 1
    assert "sell_to_vendor" not in ctx.blackboard["_skip_procedures"]
    # The stamp is dropped alongside the set entry (no leak).
    assert "sell_to_vendor" not in ctx.blackboard["_skip_procedures_at"]


@pytest.mark.asyncio
async def test_fresh_skip_survives_prune():
    """A skip still within its TTL is kept — the prune only re-arms STALE
    skips, it doesn't defeat the repeat-failure suppression itself."""
    planner = _make_planner()
    ctx = _ctx()

    await _latch_out(planner, ctx, "sell_to_vendor")

    dropped = planner._prune_expired_skips(ctx)

    assert dropped == 0
    assert "sell_to_vendor" in ctx.blackboard["_skip_procedures"]
    assert "sell_to_vendor" in ctx.blackboard["_skip_procedures_at"]


@pytest.mark.asyncio
async def test_legacy_unstamped_skip_gets_full_ttl_window():
    """A skip with no recorded stamp (legacy blackboard state) is adopted at
    'now' rather than expired immediately, so it gets a full TTL window."""
    planner = _make_planner()
    ctx = _ctx()
    # Simulate pre-existing state without the companion stamp dict.
    ctx.blackboard["_skip_procedures"] = {"craft_blacksmith"}

    dropped = planner._prune_expired_skips(ctx)

    assert dropped == 0
    assert "craft_blacksmith" in ctx.blackboard["_skip_procedures"]
    # It was adopted with a fresh stamp.
    assert "craft_blacksmith" in ctx.blackboard["_skip_procedures_at"]


@pytest.mark.asyncio
async def test_orphan_stamps_cleared_when_set_emptied():
    """When the deadlock resolver clears the skip set (in place or by pop), the
    parallel stamp dict must not linger — otherwise it grows unbounded across a
    long session."""
    planner = _make_planner()
    ctx = _ctx()

    await _latch_out(planner, ctx, "sell_to_vendor")
    assert ctx.blackboard["_skip_procedures_at"]

    # Mirror the deadlock resolver's in-place clear (deadlock.py Strategy 4).
    ctx.blackboard["_skip_procedures"].clear()

    planner._prune_expired_skips(ctx)

    assert ctx.blackboard.get("_skip_procedures_at") == {}
