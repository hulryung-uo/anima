"""Throughput/correctness test: the ChopWood *procedure* must park a depleted
or out-of-range tree in the shared ``depleted_trees`` blackboard cooldown so
the next loop iteration skips it (exactly as the skill version in
anima/skills/gathering/lumber.py already does).

Before the fix, the procedure recognised the depletion cliloc only to break
the per-swing wait — it never recorded the tree — so ``_find_nearby_tree``
(which honours ``depleted_trees``) re-selected the identical exhausted tree on
every subsequent call and the agent chopped nothing for ~20 minutes.
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


def _run_swing(ctx, tree, message):
    """Drive one ChopWood.execute swing that resolves with ``message``."""
    proc = ChopWood()
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items[0x200] = hatchet

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            social.add_speech(0x100, "System", message, 0)
        if sleeps["n"] > 50:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree", return_value=tree,
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(proc.execute(ctx))
    return result


@pytest.mark.asyncio
async def test_depleted_tree_is_parked_in_cooldown():
    """A "not enough wood" result must record the tree in depleted_trees."""
    proc = ChopWood()
    ctx = _make_ctx()
    tree = (1001, 1000, 0, 0x0CCA)
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 1000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System",
                "There's not enough wood here to harvest.", 0,
            )
        if sleeps["n"] > 50:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree", return_value=tree,
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert not result.success
    # The exhausted tree's tile must now be in the shared cooldown map so the
    # next _find_nearby_tree call skips it.
    depleted = ctx.blackboard.get("depleted_trees", {})
    assert (1001, 1000) in depleted, (
        "depleted tree was not parked in the cooldown — the loop would "
        "re-target the same exhausted tree forever"
    )


@pytest.mark.asyncio
async def test_too_far_tree_is_parked_in_cooldown():
    """An out-of-range ("too far away") result must also park the tree."""
    proc = ChopWood()
    ctx = _make_ctx()
    tree = (1001, 1000, 0, 0x0CCA)
    hatchet = MagicMock(container=0x101, graphic=0x0F43, amount=1, serial=0x200)
    ctx.perception.world.items = {0x200: hatchet}

    social = SocialState()
    ctx.perception.social = social

    clock = {"t": 3000.0}
    sleeps = {"n": 0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        sleeps["n"] += 1
        clock["t"] += 0.2
        if sleeps["n"] == 1:
            social.add_speech(
                0x100, "System", "That is too far away.", 0,
            )
        if sleeps["n"] > 50:
            clock["t"] += 100.0

    async def _use_on_target(*_a, **_k):
        return MagicMock(success=True, message="ok")

    with patch(
        "anima.procedures.chop_wood._find_nearby_tree", return_value=tree,
    ), patch(
        "anima.procedures.chop_wood.use_on_target", new=_use_on_target,
    ), patch(
        "anima.procedures.chop_wood.asyncio.sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    assert not result.success
    depleted = ctx.blackboard.get("depleted_trees", {})
    assert (1001, 1000) in depleted
