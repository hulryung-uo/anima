"""Regression: a tree-stripping chop must park the tree even when the SUCCESS
line sorts AHEAD of the depletion line in the journal.

ServUO emits both "You put some logs into your backpack." (success) and
"There's not enough wood here to harvest." (depletion) within the same
per-swing window, in an arbitrary order. ChopWood drove its tree-parking off
``result_snippet`` — the FIRST ``_RESULT_SNIPPETS`` match in journal order —
so when the success line sorted first, ``result_snippet`` was a success
snippet (NOT in ``_SKIP_TREE_SNIPPETS``) and the now-exhausted tree was never
parked in ``depleted_trees``. The next ``_find_nearby_tree`` re-selected the
identical empty tree and the agent chopped nothing until depletion happened
to sort first. The fix scans the whole swing window for a skip-tree line
(mirroring mine_ore's terminal-flag reconciliation) so parking is
order-independent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.perception.social_state import SocialState
from anima.procedures.chop_wood import ChopWood


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 1000
    ctx.perception.self_state.y = 1000
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_success_line_first_still_parks_depleted_tree():
    """SUCCESS line lands BEFORE the depletion line; the logs are credited and
    the now-empty tree must STILL be parked in depleted_trees so the loop does
    not re-target it."""
    proc = ChopWood()
    ctx = _make_ctx()
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 4000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(d):
        sleeps["n"] += 1
        clock["t"] += d
        if sleeps["n"] == 1:
            # SUCCESS line sorts FIRST this time (the inverse ordering of
            # test_chop_success_with_depletion), then the depletion line.
            social.add_speech(
                0x100, "System",
                "You put some logs into your backpack.", 0,
            )
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
            # Logs land in-pack immediately so the swing books a success.
            ctx.perception.world.items[0x300] = MagicMock(
                container=0x101, graphic=0x1BDD, amount=4,
                serial=0x300, hue=0,
            )
        if sleeps["n"] > 60:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree",
        return_value=(1001, 1000, 0, 0x0CCA),
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert result.success
    assert result.details["logs"] == 4
    # The crux: the tree must be parked even though the success snippet was the
    # FIRST journal match (so result_snippet was NOT a skip-tree snippet).
    assert (1001, 1000) in ctx.blackboard.get("depleted_trees", {}), (
        "stripped tree was not parked because the success line sorted ahead "
        "of the depletion line — the loop would re-target the empty tree"
    )
