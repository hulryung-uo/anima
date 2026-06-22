"""equip_item must not report success when the lift was rejected.

``equip_item`` lifts an item (PickUp 0x07) then wears it on a layer
(EquipItem 0x13). On a server lift-reject (0x27 LiftRej — out of range,
locked down, too heavy, or the mobile already holding something) the item
never leaves its slot. The old code fired the EquipItem anyway — against an
item it was not holding — and returned a soft ``success=True`` regardless, so
a warrior re-equipping a looted weapon after death (``_ReequipFromBackpack``)
believed it was wielded while it kept fighting bare-handed.

The guard mirrors the proven snapshot check in
``anima.actions.inventory.drag_drop`` /
``anima.action.interaction.drag_to_container``: snapshot the item's pre-lift
slot, and if it is unchanged after the lift, return ``success=False`` WITHOUT
sending the EquipItem.
"""
import asyncio
from types import SimpleNamespace

import pytest

import anima.actions.equip as equip
from anima.actions.equip import LAYER_ONE_HANDED, equip_item
from anima.perception.world_state import ItemInfo

_WEAPON = 0x9001
_BACKPACK = 0x40000015


class _StubConn:
    def __init__(self):
        self.sent: list[bytes] = []

    async def send_packet(self, pkt):
        self.sent.append(pkt)


def _ctx(items):
    world = SimpleNamespace(items={it.serial: it for it in items})
    ss = SimpleNamespace(serial=0x1, equipment={})
    return SimpleNamespace(
        conn=_StubConn(),
        perception=SimpleNamespace(self_state=ss, world=world),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(equip.asyncio, "sleep", _instant)


def test_rejected_lift_returns_failure_and_skips_equip():
    """Item stays put after PickUp (lift rejected) → no EquipItem, success False."""
    item = ItemInfo(
        serial=_WEAPON, x=0, y=0, z=0, graphic=0x143A, amount=1,
        container=_BACKPACK,
    )
    ctx = _ctx([item])
    # The stub conn never mutates the world, so the item is unchanged after the
    # (rejected) lift — exactly the LiftRej signature.
    result = asyncio.run(equip_item(ctx, _WEAPON, LAYER_ONE_HANDED))

    assert result.success is False
    # Only the PickUp (0x07) went out; the EquipItem (0x13) was suppressed.
    assert len(ctx.conn.sent) == 1
    assert ctx.conn.sent[0][0] == 0x07


def test_successful_lift_sends_equip():
    """Item disappears from view after PickUp (full lift) → EquipItem fires."""
    item = ItemInfo(
        serial=_WEAPON, x=0, y=0, z=0, graphic=0x143A, amount=1,
        container=_BACKPACK,
    )
    ctx = _ctx([item])
    ss = ctx.perception.self_state

    real_send = ctx.conn.send_packet

    async def _send_then_consume(pkt):
        await real_send(pkt)
        # ServUO removes a fully-lifted item from the world (it moves into the
        # mobile's Holding) right after the PickUp echo, then on the EquipItem
        # mirrors it onto the worn layer (0x2E -> SelfState.equipment).
        if pkt[0] == 0x07:
            ctx.perception.world.items.pop(_WEAPON, None)
        elif pkt[0] == 0x13:
            ss.equipment[LAYER_ONE_HANDED] = _WEAPON

    ctx.conn.send_packet = _send_then_consume

    result = asyncio.run(equip_item(ctx, _WEAPON, LAYER_ONE_HANDED))

    assert result.success is True
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x13 in sent_ids


def test_partial_stack_lift_counts_as_a_real_lift():
    """A split (amount decremented in place) is a real lift → EquipItem fires."""
    item = ItemInfo(
        serial=_WEAPON, x=0, y=0, z=0, graphic=0x143A, amount=5,
        container=_BACKPACK,
    )
    ctx = _ctx([item])
    ss = ctx.perception.self_state

    real_send = ctx.conn.send_packet

    async def _send_then_split(pkt):
        await real_send(pkt)
        if pkt[0] == 0x07:
            ctx.perception.world.items[_WEAPON].amount = 4
        elif pkt[0] == 0x13:
            ss.equipment[LAYER_ONE_HANDED] = _WEAPON

    ctx.conn.send_packet = _send_then_split

    result = asyncio.run(equip_item(ctx, _WEAPON, LAYER_ONE_HANDED))

    assert result.success is True
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x13 in sent_ids


def test_item_not_in_world_still_attempts_the_equip():
    """No pre-state to compare (item never tracked) → behave as before, equip."""
    ctx = _ctx([])  # empty world, nothing to snapshot
    result = asyncio.run(equip_item(ctx, _WEAPON, LAYER_ONE_HANDED))

    # Soft-success (unverified) path — the EquipItem still went out.
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x13 in sent_ids
