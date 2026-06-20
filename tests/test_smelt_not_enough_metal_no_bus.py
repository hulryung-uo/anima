"""A "not enough metal" smelt outcome must drive the sub-2 iron-pile COMBINE
even when no event bus is wired and the cliloc arrives only on the journal.

The bug: ``_smelt_flags["not_enough"]`` was set ONLY by the optional bus
callback (``_on_speech``). When ``ctx.bus is None`` (or a cliloc routes to the
journal without an ``avatar.speech_*`` publish), the flag stayed False even
though the server clearly said "There is not enough metal here." The whole
failure path keys off that flag, so a sub-2 iron pile was NEVER combined: it
fell through to the generic "Iron smelting failed (retry)" branch and
``can_start`` re-selected the identical un-smeltable 1-ore pile on the next
tick — a permanent stall. ``MineOre`` already reconciles its terminal swing
flags from the journal (commit e049b68); ``smelt_ore`` did not.

This test runs the no-bus path and pins that the small pile is merged onto a
larger same-type stack (lift + drop-onto-stack) instead of looping forever.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.client.packets import build_drop_item, build_pick_up
from anima.perception.social_state import SocialState
from anima.procedures.smelt_ore import SmeltOre
from anima.skills.crafting.smelt import ORE_GRAPHICS


def _make_ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = 2500
    ss.y = 550
    ss.z = 15
    ss.equipment = {0x15: 0x101}  # backpack serial
    ctx.perception.world.items = {}
    ctx.perception.social = SocialState()
    ctx.conn.send_packet = AsyncMock()
    # NO BUS — the whole point of this test. The "not enough metal" cliloc
    # reaches the procedure only through the journal.
    ctx.bus = None
    # Pre-exclude the big pile from the swing-candidate list so the 1-piece
    # pile is the one targeted (and thus the one that hits the combine branch).
    # The combine-target scan does NOT honour this set.
    ctx.blackboard = {"_small_iron_ore_serials": {0x301}}
    return ctx


@pytest.mark.asyncio
async def test_small_iron_pile_combines_without_bus():
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    small = MagicMock(
        container=0x101, graphic=ore_graphic, amount=1, serial=0x300, hue=0,
    )
    big = MagicMock(
        container=0x101, graphic=ore_graphic, amount=1, serial=0x301, hue=0,
    )
    ctx.perception.world.items = {0x300: small, 0x301: big}

    social = ctx.perception.social
    import anima.procedures.smelt_ore as mod

    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        clock["t"] += 0.2

    async def _use_on_target(*_a, **_k):
        # The smelt swing fires; the server replies "not enough metal" — but
        # ONLY on the journal (no bus). add_speech stamps the entry with the
        # (mocked) current time, which is >= smelt_start, so the reconcile sees
        # it.
        social.add_speech(
            0x100, "System", "There is not enough metal here to make an ingot.", 0,
        )
        return MagicMock(success=True, message="ok")

    combine_via_double_click = {"hit": False}

    async def _use_on_object(*_a, **_k):
        combine_via_double_click["hit"] = True
        return MagicMock(success=True, message="ok")

    with patch.object(
        mod, "_find_forge_dynamic", return_value=None,
    ), patch.object(
        mod, "_find_forge_static", return_value=(2500, 550, 15, 4017),
    ), patch.object(
        mod, "use_on_target", new=_use_on_target,
    ), patch.object(
        mod, "use_on_object", new=_use_on_object,
    ), patch.object(
        mod.asyncio, "sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    # The combine must use a lift + drop-onto-stack merge, never the smelt
    # target cursor (use_on_object).
    assert not combine_via_double_click["hit"], (
        "small-pile combine wrongly used the smelt target cursor"
    )

    sent = [c.args[0] for c in ctx.conn.send_packet.call_args_list]
    assert build_pick_up(0x300, 1) in sent, (
        "no-bus path failed to reconcile 'not enough metal' from the journal: "
        "the small iron pile was never lifted to combine"
    )
    assert build_drop_item(0x300, container=0x301) in sent, (
        "small pile was not dropped onto the larger iron stack to merge"
    )
    # And it re-suggests smelting now that the piles are merged.
    assert result.next_suggestion == "smelt_ore"


@pytest.mark.asyncio
async def test_colored_ore_blacklisted_immediately_without_bus():
    """A colored hue with enough quantity that the server still refuses
    ("not enough metal") must be blacklisted on the FIRST failure on the
    no-bus path — not after a 3-strike counter."""
    proc = SmeltOre()
    ctx = _make_ctx()
    ctx.blackboard = {}  # no pre-seeded small set; colored ore here

    ore_graphic = next(iter(ORE_GRAPHICS))
    colored = MagicMock(
        container=0x101, graphic=ore_graphic, amount=5, serial=0x400, hue=2419,
    )
    ctx.perception.world.items = {0x400: colored}

    social = ctx.perception.social
    import anima.procedures.smelt_ore as mod

    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    async def fake_sleep(_d):
        clock["t"] += 0.2

    async def _use_on_target(*_a, **_k):
        social.add_speech(
            0x100, "System", "There is not enough metal here to make an ingot.", 0,
        )
        return MagicMock(success=True, message="ok")

    with patch.object(
        mod, "_find_forge_dynamic", return_value=None,
    ), patch.object(
        mod, "_find_forge_static", return_value=(2500, 550, 15, 4017),
    ), patch.object(
        mod, "use_on_target", new=_use_on_target,
    ), patch.object(
        mod, "use_on_object", new=AsyncMock(return_value=MagicMock(success=True)),
    ), patch.object(
        mod.asyncio, "sleep", new=fake_sleep,
    ), patch(
        "time.time", new=fake_time,
    ):
        result = await proc.execute(ctx)

    # First failure already blacklists the hue (immediate_blacklist), without
    # the bus this used to need 3 strikes.
    assert 2419 in ctx.blackboard.get("_unsmelable_ore_hues", set()), (
        "colored hue not blacklisted on first 'not enough metal' (no-bus path)"
    )
    assert result.reason.name == "PERMANENT"
