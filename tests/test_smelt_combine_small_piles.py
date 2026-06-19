"""A sub-2 iron ore pile that the shard refuses to smelt ("not enough
metal") must be COMBINED into a larger same-type pile via a real
drag-and-drop stack merge — not by double-clicking the ore (which opens
the smelt target cursor, never a combine).

Before the fix, smelt_ore.execute() tried to merge the small pile with
``use_on_object(ctx, ore_serial, other_iron.serial)``. ``use_on_object``
double-clicks the ore, and in ServUO double-clicking ore begins a *smelt*
(Ore.OnDoubleClick -> Smelt), so the follow-up target landed on another
ore stack with the smelt cursor and merged nothing. The small pile was
left untouched and immediately blacklisted, wasting the ore. UO merges
same-type stacks with a lift + drop-onto-stack, which is what this test
pins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.client.packets import build_drop_item, build_pick_up
from anima.perception.social_state import SocialState
from anima.procedures.smelt_ore import SmeltOre
from anima.skills.crafting.smelt import ORE_GRAPHICS


class _FakeBus:
    """Minimal synchronous event bus: subscribe/unsubscribe + emit."""

    def __init__(self):
        self._subs: dict[int, tuple[str, callable]] = {}
        self._next = 0

    def subscribe(self, topic, handler):
        self._next += 1
        self._subs[self._next] = (topic, handler)
        return self._next

    def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)

    def emit(self, topic, data):
        for t, h in list(self._subs.values()):
            if t == topic:
                h(topic, data)


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
    ctx.bus = _FakeBus()
    # Pre-exclude the big pile from the swing-candidate list so the 1-piece
    # pile is the one targeted (and thus the one that hits the combine
    # branch). The combine-target scan does NOT honour this set.
    ctx.blackboard = {"_small_iron_ore_serials": {0x301}}
    return ctx


@pytest.mark.asyncio
async def test_small_iron_pile_combines_via_drag_drop():
    proc = SmeltOre()
    ctx = _make_ctx()

    ore_graphic = next(iter(ORE_GRAPHICS))
    small = MagicMock(
        container=0x101, graphic=ore_graphic, amount=1, serial=0x300, hue=0,
    )
    # Big pile: amount 1 so the pre-seeded _small_iron_ore_serials entry
    # excludes it from the swing candidates, leaving `small` as the target.
    # It is still a valid same-type combine target at the failure point.
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
        # The smelt swing fires; the server replies "not enough metal".
        text = "There is not enough metal here."
        social.add_speech(0x100, "System", text, 0)
        ctx.bus.emit("avatar.speech_heard", {"text": text})
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

    # The combine must NOT be attempted by double-clicking the ore (smelt
    # cursor): use_on_object stays valid only for ore -> forge.
    assert not combine_via_double_click["hit"], (
        "small-pile combine wrongly used the smelt target cursor "
        "(use_on_object) instead of a drag/drop stack merge"
    )

    # It MUST have lifted the small pile and dropped it onto the big pile's
    # serial (container == big.serial), the UO same-type stack merge.
    sent = [c.args[0] for c in ctx.conn.send_packet.call_args_list]
    assert build_pick_up(0x300, 1) in sent, "did not lift the small ore pile"
    assert build_drop_item(0x300, container=0x301) in sent, (
        "did not drop the small pile onto the larger iron stack to merge"
    )

    # And it should re-suggest smelting now that the piles are merged.
    assert result.next_suggestion == "smelt_ore"
