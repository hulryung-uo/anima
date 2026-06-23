"""Concurrency guard: UoConnection._send_raw must serialize concurrent sends
and survive a reconnect/close that races an in-flight send.

Multiple coroutines share one UoConnection and call send_packet concurrently
(the planner/engine loop, inspect_self, and ping_loop all run together in
main.py's TaskGroup). The send path write()s then awaits drain(); without a
lock two coroutines interleave there, and reading self._writer twice (assert
then write) was a TOCTOU that — when a concurrent _close() nulled the writer
mid-send — raised AttributeError (or AssertionError when -O strips the assert)
instead of a clean ConnectionError the reconnect loop knows how to handle.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.client.connection import UoConnection


class _SlowWriter:
    """StreamWriter stub whose drain() suspends, so we can observe overlap and
    inject a concurrent close while a send is parked inside drain()."""

    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []
        self.in_drain = 0
        self.max_concurrent_drain = 0
        self._gate = asyncio.Event()

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    async def wait_closed(self) -> None:
        return None

    def write(self, data: bytes) -> None:
        if self.closed:
            raise AssertionError("write() called on a closed writer")
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        self.in_drain += 1
        self.max_concurrent_drain = max(self.max_concurrent_drain, self.in_drain)
        try:
            # Park here until released, simulating real flow-control backpressure.
            await self._gate.wait()
        finally:
            self.in_drain -= 1

    def release(self) -> None:
        self._gate.set()


@pytest.mark.asyncio
async def test_concurrent_sends_do_not_overlap_in_drain():
    """Two coroutines sending at once must not both be inside drain()."""
    conn = UoConnection(timeout=1.0)
    writer = _SlowWriter()
    conn._writer = writer  # type: ignore[assignment]

    t1 = asyncio.create_task(conn.send_packet(b"\x01aaa"))
    t2 = asyncio.create_task(conn.send_packet(b"\x02bbb"))

    # Let both tasks run; the first acquires the lock and parks in drain(),
    # the second must block on the lock (never enter drain()).
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert writer.in_drain == 1, "second sender entered drain() before first finished"

    writer.release()
    await asyncio.gather(t1, t2)

    # Never more than one concurrent drain; both frames went out, in order.
    assert writer.max_concurrent_drain == 1
    assert writer.writes == [b"\x01aaa", b"\x02bbb"]


@pytest.mark.asyncio
async def test_close_racing_send_raises_connection_error_not_attribute_error():
    """A _close() that nulls the writer mid-send must yield ConnectionError."""
    conn = UoConnection(timeout=1.0)
    writer = _SlowWriter()
    conn._writer = writer  # type: ignore[assignment]

    # Sender #1 grabs the lock and parks in drain().
    t1 = asyncio.create_task(conn.send_packet(b"\x01first"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert writer.in_drain == 1

    # Sender #2 queues behind the lock while a reconnect closes the connection.
    t2 = asyncio.create_task(conn.send_packet(b"\x02second"))
    await asyncio.sleep(0)

    # Reconnect path: _close() nulls the writer. Release the parked drain so the
    # first send completes and the lock hands off to the second sender, which
    # now observes writer is None.
    await conn._close()
    writer.release()

    await t1  # first send already had its writer snapshot; completes cleanly.

    with pytest.raises(ConnectionError):
        await t2


@pytest.mark.asyncio
async def test_send_on_closed_connection_is_connection_error():
    """Sending when the writer is already gone is a clean ConnectionError."""
    conn = UoConnection(timeout=1.0)
    conn._writer = None
    with pytest.raises(ConnectionError):
        await conn.send_packet(b"\x01x")
