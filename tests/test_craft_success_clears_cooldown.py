"""A landed blacksmith craft must clear the legacy timed cooldown latches the
material/location failure ladders armed — not just the failure counters.

The bug: ``_craft_bs_material_cooldown`` (armed for up to 1800s after >=3
"insufficient metal" misses) and ``_craft_bs_location_cooldown`` (120s after
>=3 walk-to-forge misses) are honored in ``can_start`` and in the planner's
profession gate, but the success path only reset the ``_craft_bs_*_fails``
counters and the CircuitBreaker. The timestamp latch therefore survived a
successful batch, so ``can_start`` kept refusing to re-enter ``craft_blacksmith``
for the rest of that window even though the batch that just landed proved the
material context and the station are usable again — a cooldown that never
resets after its trigger cleared.
"""

import time
from types import SimpleNamespace

import pytest

import anima.procedures.craft_blacksmith as cb
from anima.procedures.craft_blacksmith import CraftBlacksmith


def _make_ctx():
    ss = SimpleNamespace(
        equipment={0x15: 0x40000015},
        skills={},
        gumps={},
        x=100,
        y=100,
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items={}),
            social=SimpleNamespace(journal=[], recent=lambda count=8: []),
        ),
        conn=SimpleNamespace(connected=True),
        bus=None,
        blackboard={},
    )


@pytest.mark.asyncio
async def test_successful_craft_clears_stale_material_and_location_cooldowns(monkeypatch):
    proc = CraftBlacksmith()

    # Stub everything except the post-batch success classification under test.
    monkeypatch.setattr(cb, "find_in_backpack", lambda ctx, g: [object()])  # tongs
    monkeypatch.setattr(cb, "_has_anvil_and_forge", lambda ctx: True)
    monkeypatch.setattr(
        CraftBlacksmith, "_pick_recipe",
        lambda self, ctx: ("Longsword", 3, 7, 12),  # ingot_cost = 12
    )

    async def _noop_close(self, ctx):
        return None
    monkeypatch.setattr(CraftBlacksmith, "_close_all_gumps", _noop_close)

    async def _nav_ok(self, ctx, grp, idx):
        return None  # first attempt sent, result pending
    monkeypatch.setattr(CraftBlacksmith, "_open_gump_and_craft", _nav_ok)

    # The single attempt lands a craft; ingots then fall below the recipe cost
    # so the batch stops cleanly (out_of_ingots) right after one success.
    state = {"resolved": False}

    async def _result(self, ctx, journal_mark, timeout=cb._RESULT_TIMEOUT_S):
        state["resolved"] = True
        return "success", "You create the item."
    monkeypatch.setattr(CraftBlacksmith, "_await_craft_result", _result)

    monkeypatch.setattr(
        cb, "_count_iron_ingots",
        lambda ctx: 5 if state["resolved"] else 20,
    )

    ctx = _make_ctx()
    # Both legacy timed cooldowns are armed well into the future (the state a
    # >=3-fail ladder would leave behind), and their fail counters are parked.
    future = time.time() + 1800
    ctx.blackboard["_craft_bs_material_cooldown"] = future
    ctx.blackboard["_craft_bs_location_cooldown"] = future
    ctx.blackboard["_craft_bs_material_fails"] = 3
    ctx.blackboard["_craft_bs_location_fails"] = 3

    result = await proc.execute(ctx)

    # The batch landed at least one item.
    assert result.success is True

    # The stale timed latches are cleared (not left parked in the future), so a
    # later tick can re-enter the craft loop.
    assert ctx.blackboard.get("_craft_bs_material_cooldown", 0) <= time.time()
    assert ctx.blackboard.get("_craft_bs_location_cooldown", 0) <= time.time()
    assert ctx.blackboard.get("_craft_bs_material_fails") == 0
    assert ctx.blackboard.get("_craft_bs_location_fails") == 0

    # And can_start no longer refuses purely because of the (now-cleared)
    # cooldowns: with tongs + ingots + forge stubbed present it accepts again.
    monkeypatch.setattr(cb, "_has_anvil_and_forge_nearby", lambda ctx: True)
    monkeypatch.setattr(cb, "_count_iron_ingots", lambda ctx: 20)
    monkeypatch.setattr(cb, "_count_crafted_in_pack", lambda ctx: 0)
    assert await proc.can_start(ctx) is True
