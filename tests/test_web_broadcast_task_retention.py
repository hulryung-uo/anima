"""WebServer.broadcast must keep strong refs to its send tasks and reap dead
clients whose send fails.

The old broadcast did ``asyncio.ensure_future(ws.send_str(data))`` and threw
the returned task away. asyncio only holds a *weak* reference to a scheduled
task, so under GC pressure the send coroutine could be collected mid-await and
the dashboard frame would never reach the browser. And because the send was
never awaited, a failure (client closed between the ``ws.closed`` check and the
actual write) raised inside an orphan task nobody observed — so a client that
died exactly there was never discarded from ``_clients`` and kept getting
(failing) sends forever.

These tests use a fake WebSocket so no real network/event-loop teardown is
involved; they assert (1) the send actually completes against a live client,
(2) the in-flight task is retained while pending and dropped on completion, and
(3) a client whose send raises is reaped from ``_clients``.
"""

import asyncio

from anima.web.command_bus import CommandBus
from anima.web.server import WebServer


class _FakeWS:
    """Minimal stand-in for aiohttp's WebSocketResponse."""

    def __init__(self, *, closed: bool = False, fail: bool = False) -> None:
        self.closed = closed
        self._fail = fail
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        # Yield once so the send genuinely suspends — this is the window in
        # which a weakly-referenced orphan task could be GC'd.
        await asyncio.sleep(0)
        if self._fail:
            raise ConnectionResetError("client gone")
        self.sent.append(data)


def _server() -> WebServer:
    return WebServer(port=0, command_bus=CommandBus(), conn=None)


async def _drain(srv: WebServer) -> None:
    """Let all scheduled send tasks run to completion."""
    while srv._send_tasks:
        await asyncio.gather(*list(srv._send_tasks), return_exceptions=True)
        await asyncio.sleep(0)


async def test_broadcast_delivers_frame_to_live_client() -> None:
    srv = _server()
    ws = _FakeWS()
    srv._clients.add(ws)

    srv.broadcast({"status": {"hp": 7}})

    # The send is scheduled, not awaited synchronously — it must be tracked.
    assert srv._send_tasks, "broadcast dropped the send task reference"

    await _drain(srv)
    assert len(ws.sent) == 1
    assert "hp" in ws.sent[0]
    # Completed tasks are released by the done-callback.
    assert not srv._send_tasks


async def test_broadcast_retains_task_until_it_completes() -> None:
    srv = _server()
    ws = _FakeWS()
    srv._clients.add(ws)

    srv.broadcast({"status": {"hp": 1}})
    # While the send is still pending (it awaits sleep(0)), the task is held by
    # the server so it cannot be garbage-collected mid-await.
    assert len(srv._send_tasks) == 1
    (task,) = tuple(srv._send_tasks)
    assert not task.done()

    await _drain(srv)
    assert task.done()
    assert not srv._send_tasks


async def test_broadcast_reaps_client_whose_send_fails() -> None:
    srv = _server()
    good = _FakeWS()
    bad = _FakeWS(fail=True)
    srv._clients.add(good)
    srv._clients.add(bad)

    srv.broadcast({"status": {"hp": 5}})
    await _drain(srv)

    # The failing client is reaped; the healthy one stays and received the frame.
    assert bad not in srv._clients
    assert good in srv._clients
    assert len(good.sent) == 1


async def test_broadcast_skips_already_closed_client() -> None:
    srv = _server()
    closed = _FakeWS(closed=True)
    srv._clients.add(closed)

    srv.broadcast({"status": {"hp": 9}})
    # A closed client is filtered before scheduling — no task, no send.
    assert not srv._send_tasks
    await _drain(srv)
    assert closed not in srv._clients
    assert closed.sent == []


async def test_broadcast_no_clients_is_a_noop() -> None:
    srv = _server()
    srv.broadcast({"status": {"hp": 3}})
    assert not srv._send_tasks
    # _last_snapshot is still updated so a freshly-connected client gets state.
    assert "hp" in srv._last_snapshot
