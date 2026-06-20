"""wait_for_gump must not crash when the matching gump vanishes mid-wait.

Regression: ``wait_for_gump`` selected the gump to return with a bare

    gump_id = next(gid for gid in reversed(ss.gumps) if gid not in exclude)

``next()`` with no default. ``ok`` reflects the predicate at the instant the
bus wait resolved, but ``ss.gumps`` is mutated by packet handlers that run
concurrently on the same event loop. A server-side CloseGump (0xBF sub 0x04 ->
``ss.gumps.pop``) — or a gump re-keyed by a fresh 0xB0/0xDD — that lands
between the wait waking and ``wait_for_gump`` resuming leaves NO eligible
(non-excluded) gump by selection time. The bare ``next()`` then raises
``StopIteration``, which PEP 479 converts to
``RuntimeError: coroutine raised StopIteration``.

That RuntimeError is fatal in practice: the helper procedures that drive
gump-based flows (``craft_via_gump`` callers — blacksmithy/tinker/carpentry,
banking, vendor) run inside ad-hoc ``run()`` methods that, unlike
``Procedure.run``, have no surrounding try/except, and the planner's
``_run_procedure`` only catches ``TimeoutError``/``CancelledError`` — so the
crash escapes to the planner tick and kills the session over a benign timing
race. ``wait_for_gump`` must degrade to its normal "Gump timeout" failure
(which every caller already handles) instead.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from anima.actions.gump import wait_for_gump
from anima.core.bus import EventBus
from anima.perception.gump import GumpData


def _gump(gump_id: int) -> GumpData:
    return GumpData(serial=gump_id, gump_id=gump_id, x=0, y=0, layout="", text_lines=[])


def test_gump_popped_between_wait_resolving_and_selection_does_not_crash():
    """The bus wait reports satisfied, but the gump is gone by selection time.

    Models the TOCTOU directly: a concurrent packet handler emptied
    ``ss.gumps`` after the predicate had been satisfied. ``wait_for_condition``
    still returns True (it observed the gump at some instant), and
    ``wait_for_gump`` must NOT raise — it returns a graceful timeout failure.
    """
    ss = SimpleNamespace(gumps={100: _gump(100)})

    class _RacingBus:
        async def wait_for_condition(self, predicate, timeout=5.0):  # noqa: ARG002
            # A concurrent handler (e.g. 0xBF CloseGump) pops the gump in the
            # window between the wait resolving and wait_for_gump resuming.
            ss.gumps.clear()
            return True

    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss), bus=_RacingBus())

    # On HEAD this raised RuntimeError("coroutine raised StopIteration").
    result = asyncio.run(wait_for_gump(ctx, timeout=0.1))
    assert not result.success
    assert "timeout" in result.message.lower()


def test_real_bus_close_during_wait_does_not_crash():
    """Faithful end-to-end variant using the real EventBus.

    A fresh gump arrives (waking the wait), then a server CloseGump pops it
    before the waiter is rescheduled. The wait still resolved True, so the
    selection step must tolerate the now-empty (eligible) gump set.
    """
    ss = SimpleNamespace(gumps={})
    bus = EventBus()
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss), bus=bus)

    async def scenario():
        wait_task = asyncio.create_task(wait_for_gump(ctx, timeout=1.0))
        # Let wait_for_gump subscribe its "*" predicate listener.
        await asyncio.sleep(0)

        # Fresh gump arrives -> the bus wait's predicate is satisfied and its
        # internal asyncio.Event is set inside this publish() call.
        ss.gumps[100] = _gump(100)
        bus.publish("avatar.gump_opened", {"gump_id": 100})

        # Server immediately closes that gump (0xBF sub 0x04) before the woken
        # wait_for_gump coroutine gets a chance to run its selection step.
        ss.gumps.pop(100, None)

        return await wait_task

    result = asyncio.run(scenario())
    # Must not crash; either it caught the gump before the pop (success) or it
    # degraded to a graceful timeout — never a RuntimeError/StopIteration.
    assert isinstance(result.success, bool)
    if not result.success:
        assert "timeout" in result.message.lower()
