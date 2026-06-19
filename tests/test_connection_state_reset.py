"""Lifecycle guard: UoConnection must reset protocol state on (re)connect/close.

The two-phase login flow runs phase 1 in raw mode, calls _close(), then
_connect() for phase 2 (game mode). A reconnect can also reuse a connection
object. If _close()/_connect() leave _game_mode=True or leftover bytes in the
framing buffers, the next session decodes the plaintext login stream through
the Huffman path and/or prepends stale compressed bytes — corrupting the
stream and wedging the receive loop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from anima.client.connection import UoConnection


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    async def wait_closed(self) -> None:
        return None


class _FakeReader:
    pass


@pytest.mark.asyncio
async def test_close_resets_game_mode_and_buffers():
    """After a game-mode session, _close() must scrub protocol state."""
    conn = UoConnection(timeout=1.0)
    conn._game_mode = True
    conn._recv_buffer.extend(b"\xaa\xbb")          # half-parsed decompressed frame
    conn._compressed_buffer.extend(b"\x01\x02\x03")  # half-decoded compressed bytes
    conn._writer = _FakeWriter()  # type: ignore[assignment]
    conn._reader = _FakeReader()  # type: ignore[assignment]

    await conn._close()

    assert conn._game_mode is False
    assert len(conn._recv_buffer) == 0
    assert len(conn._compressed_buffer) == 0
    assert conn._writer is None
    assert conn._reader is None


@pytest.mark.asyncio
async def test_close_is_idempotent():
    """_close() on an already-closed connection must not raise."""
    conn = UoConnection(timeout=1.0)
    await conn._close()
    await conn._close()
    assert conn._game_mode is False


@pytest.mark.asyncio
async def test_connect_starts_in_raw_mode_with_empty_buffers():
    """_connect() must reset state left over from a prior game session.

    Simulates the phase-1 -> _close() -> phase-2 transition reusing the same
    object: the leftover game-mode flag and compressed bytes from the previous
    session must be gone so phase 1 reads the plaintext ServerList correctly.
    """
    conn = UoConnection(timeout=1.0)
    # Pretend a previous game session left dirty state behind.
    conn._game_mode = True
    conn._recv_buffer.extend(b"\xde\xad")
    conn._compressed_buffer.extend(b"\xbe\xef")

    async def _fake_open_connection(host, port):
        return _FakeReader(), _FakeWriter()

    with patch(
        "anima.client.connection.asyncio.open_connection",
        new=_fake_open_connection,
    ):
        await conn._connect("127.0.0.1", 2593)

    assert conn._game_mode is False
    assert len(conn._recv_buffer) == 0
    assert len(conn._compressed_buffer) == 0


@pytest.mark.asyncio
async def test_recv_after_reconnect_uses_raw_framing():
    """End-to-end: dirty game-mode session -> _close() -> _connect() -> recv.

    recv_packet() must route through the raw (login) framing path after the
    reconnect, returning the plaintext fixed-length packet intact instead of
    feeding it to the Huffman decoder.
    """
    conn = UoConnection(timeout=1.0)
    conn._game_mode = True
    conn._compressed_buffer.extend(b"\x99\x99\x99")  # stale bytes from old session

    await conn._close()

    async def _fake_open_connection(host, port):
        return _FakeReader(), _FakeWriter()

    with patch(
        "anima.client.connection.asyncio.open_connection",
        new=_fake_open_connection,
    ):
        await conn._connect("127.0.0.1", 2593)

    # 0xA7 is a fixed-length (4) login-phase packet. Feed it via a stub reader.
    payload = bytes([0xA7, 0x11, 0x22, 0x33])
    chunks = [payload[:1], payload[1:]]

    class _RawReader:
        async def readexactly(self, n: int) -> bytes:
            out = bytearray()
            while len(out) < n and chunks:
                out.extend(chunks.pop(0))
            return bytes(out[:n])

    conn._reader = _RawReader()  # type: ignore[assignment]

    packet_id, data = await conn.recv_packet(timeout=1.0)

    assert conn._game_mode is False  # never silently re-entered game mode
    assert packet_id == 0xA7
    assert data == payload
