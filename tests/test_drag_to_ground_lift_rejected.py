"""drag_to_ground must not report success when the lift was rejected.

``drag_to_ground`` lifts an item (PickUp 0x07) then drops it on the ground
(DropItem 0x08). On a server lift-reject (0x27 LiftRej — out of range, locked
down, too heavy, or the mobile already holding something) the item never
leaves its slot. The old code fired the DropItem anyway — against an item it
was not holding — and returned ``True`` regardless, so ``move_item_on_ground``
believed the item advanced and re-dragged from the same spot every iteration.

The guard mirrors the proven snapshot check in ``drag_to_container`` /
``anima.actions.inventory.drag_drop``: snapshot the item's pre-lift slot, and
if it is unchanged after the lift, return ``False`` WITHOUT sending the drop.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.action import interaction
from anima.action.interaction import drag_to_ground
from anima.perception.world_state import ItemInfo

_ITEM = 0x9001


class _StubConn:
    def __init__(self):
        self.sent: list[bytes] = []

    async def send_packet(self, pkt):
        self.sent.append(pkt)


def _ctx(items):
    world = SimpleNamespace(items={it.serial: it for it in items})
    ss = SimpleNamespace(x=100, y=100)
    return SimpleNamespace(
        conn=_StubConn(),
        perception=SimpleNamespace(self_state=ss, world=world),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr(interaction.asyncio, "sleep", _instant)


def test_rejected_lift_returns_false_and_skips_drop():
    """Item stays put after PickUp (lift rejected) → no DropItem, returns False."""
    item = ItemInfo(
        serial=_ITEM, x=100, y=100, z=0, graphic=0x1BF2, amount=10,
        container=0,
    )
    ctx = _ctx([item])
    # The stub conn never mutates the world, so the item is unchanged after
    # the (rejected) lift — exactly the LiftRej signature.
    ok = asyncio.run(drag_to_ground(ctx, _ITEM, 10, 101, 100, 0))

    assert ok is False
    # Only the PickUp (0x07) went out; the DropItem (0x08) was suppressed.
    assert len(ctx.conn.sent) == 1
    assert ctx.conn.sent[0][0] == 0x07


def test_successful_lift_sends_drop_and_returns_true():
    """Item disappears from view after PickUp (full lift) → DropItem fires, True."""
    item = ItemInfo(
        serial=_ITEM, x=100, y=100, z=0, graphic=0x1BF2, amount=10,
        container=0,
    )
    ctx = _ctx([item])

    real_send = ctx.conn.send_packet

    async def _send_then_consume(pkt):
        await real_send(pkt)
        # ServUO removes a fully-lifted item from the world (it moves into
        # the mobile's Holding) right after the PickUp echo.
        if pkt[0] == 0x07:
            ctx.perception.world.items.pop(_ITEM, None)

    ctx.conn.send_packet = _send_then_consume

    ok = asyncio.run(drag_to_ground(ctx, _ITEM, 10, 101, 100, 0))

    assert ok is True
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x08 in sent_ids


def test_partial_stack_lift_counts_as_a_real_lift():
    """A split (amount decremented in place) is a real lift → DropItem, True."""
    item = ItemInfo(
        serial=_ITEM, x=100, y=100, z=0, graphic=0x1BF2, amount=50,
        container=0,
    )
    ctx = _ctx([item])

    real_send = ctx.conn.send_packet

    async def _send_then_split(pkt):
        await real_send(pkt)
        if pkt[0] == 0x07:
            # Partial lift: source stack stays in place, amount shrinks.
            ctx.perception.world.items[_ITEM].amount = 30

    ctx.conn.send_packet = _send_then_split

    ok = asyncio.run(drag_to_ground(ctx, _ITEM, 20, 101, 100, 0))

    assert ok is True
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x08 in sent_ids


def test_item_not_in_world_still_attempts_the_drop():
    """No pre-state to compare (item never tracked) → behave as before, send drop."""
    ctx = _ctx([])  # empty world
    ok = asyncio.run(drag_to_ground(ctx, _ITEM, 1, 101, 100, 0))

    assert ok is True
    sent_ids = [p[0] for p in ctx.conn.sent]
    assert 0x07 in sent_ids and 0x08 in sent_ids


def test_too_far_returns_false_before_any_packet():
    """Drop target out of the 2-tile drag range → fail with no packets sent."""
    item = ItemInfo(serial=_ITEM, x=100, y=100, z=0, container=0)
    ctx = _ctx([item])
    ok = asyncio.run(drag_to_ground(ctx, _ITEM, 1, 110, 100, 0))
    assert ok is False
    assert ctx.conn.sent == []
