"""move_item_on_ground must not hang on a stuck/desynced stamina value.

The low-stamina recovery wait used to be ``while ss.stam < ss.stam_max*0.3:
await asyncio.sleep(1.0)`` with NO deadline and NO bound. If the server stops
pushing stat updates (0x11) the value never crosses the threshold and the
drag hangs forever. The fix mirrors the proven ``go_to_fatigue_wait`` loop in
anima.action.movement: a fixed max-iteration loop that gives up after a
bounded number of 1s ticks.
"""
import asyncio
from types import SimpleNamespace

import pytest

from anima.action import interaction
from anima.action.interaction import move_item_on_ground
from anima.perception.world_state import ItemInfo

_ITEM = 0x9001


class _StubConn:
    def __init__(self):
        self.sent: list[bytes] = []

    async def send_packet(self, pkt):
        self.sent.append(pkt)


def _ctx(item, *, stam, stam_max):
    world = SimpleNamespace(items={item.serial: item})
    ss = SimpleNamespace(x=100, y=100, stam=stam, stam_max=stam_max)
    return SimpleNamespace(
        conn=_StubConn(),
        perception=SimpleNamespace(self_state=ss, world=world),
    )


@pytest.fixture(autouse=True)
def _count_sleeps(monkeypatch):
    """Make asyncio.sleep instant and count calls (proves the loop is bounded)."""
    calls = {"n": 0}

    async def _instant(_):
        calls["n"] += 1

    monkeypatch.setattr(interaction.asyncio, "sleep", _instant)
    return calls


@pytest.fixture(autouse=True)
def _stub_go_to(monkeypatch):
    """Neutralize movement.go_to (imported lazily inside move_item_on_ground)."""
    import anima.action.movement as movement

    async def _noop(*_a, **_k):
        return True

    monkeypatch.setattr(movement, "go_to", _noop)


def _drag_advances_item_to_target(monkeypatch):
    """Stub drag_to_ground so one drag lands the item on target, then loop ends."""
    async def _drag(ctx, serial, amount, x, y, z):
        it = ctx.perception.world.items[serial]
        it.x, it.y = x, y
        # keep player adjacent so the next iteration sees remaining==0
        ctx.perception.self_state.x, ctx.perception.self_state.y = x, y
        return True

    monkeypatch.setattr(interaction, "drag_to_ground", _drag)


def test_stuck_low_stamina_does_not_hang(_count_sleeps, monkeypatch):
    """Stamina pinned below the 30% threshold → wait is bounded, returns True."""
    item = ItemInfo(serial=_ITEM, x=100, y=100, z=0, container=0)
    # 5/100 = 5% < 30%, and it NEVER recovers (desynced stat stream).
    ctx = _ctx(item, stam=5, stam_max=100)
    _drag_advances_item_to_target(monkeypatch)

    ok = asyncio.run(
        move_item_on_ground(ctx, _ITEM, 1, 101, 100, 0)
    )

    assert ok is True
    # The bounded wait must cap at 30 ticks (mirrors go_to_fatigue_wait),
    # NOT spin forever. One drag iteration → exactly one bounded wait.
    assert _count_sleeps["n"] == 30


def test_recovered_stamina_breaks_early(_count_sleeps, monkeypatch):
    """Stamina climbing back over threshold ends the wait before the cap."""
    item = ItemInfo(serial=_ITEM, x=100, y=100, z=0, container=0)
    ctx = _ctx(item, stam=5, stam_max=100)
    _drag_advances_item_to_target(monkeypatch)

    ss = ctx.perception.self_state
    real_sleep = interaction.asyncio.sleep

    async def _recover(delay):
        await real_sleep(delay)
        ss.stam = 100  # next threshold check passes → break

    monkeypatch.setattr(interaction.asyncio, "sleep", _recover)

    ok = asyncio.run(move_item_on_ground(ctx, _ITEM, 1, 101, 100, 0))

    assert ok is True
    # Recovered after the first tick → well under the 30-tick cap.
    assert _count_sleeps["n"] == 1


def test_unknown_stam_max_skips_wait(_count_sleeps, monkeypatch):
    """stam_max<=0 (stamina unknown) → no wait at all, returns True."""
    item = ItemInfo(serial=_ITEM, x=100, y=100, z=0, container=0)
    ctx = _ctx(item, stam=0, stam_max=0)
    _drag_advances_item_to_target(monkeypatch)

    ok = asyncio.run(move_item_on_ground(ctx, _ITEM, 1, 101, 100, 0))

    assert ok is True
    assert _count_sleeps["n"] == 0
