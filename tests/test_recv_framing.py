"""Tests for the game-mode packet framing/buffer layer (UoConnection)."""

import asyncio

import pytest

from anima.client.connection import UoConnection


class _NoData:
    """A stand-in StreamReader that yields no bytes.

    If the framing loop ever falls through to a TCP read it gets EOF and the
    method raises ConnectionError, so a hang can never masquerade as success.
    """

    async def read(self, _n: int) -> bytes:
        return b""


def _make_conn(recv_buffer: bytes) -> UoConnection:
    conn = UoConnection(timeout=1.0)
    conn._game_mode = True
    conn._reader = _NoData()  # type: ignore[assignment]
    conn._recv_buffer.extend(recv_buffer)
    return conn


def test_malformed_varlen_frame_resyncs_to_next_packet():
    """A variable-length frame declaring total_len < 3 must not wedge the loop.

    0xA8 is a variable-length packet. A total_len of 2 is impossible (the
    3-byte ID+length header alone is larger). The receiver must discard the
    bogus header and recover the well-formed fixed-length packet (0xA7, len 4)
    that follows — instead of blocking on the socket forever.
    """
    bad_header = bytes([0xA8, 0x00, 0x02])  # var-len, total_len = 2 (impossible)
    good_packet = bytes([0xA7, 0x11, 0x22, 0x33])  # 0xA7 is fixed length 4
    conn = _make_conn(bad_header + good_packet)

    packet_id, data = asyncio.run(conn._recv_game_packet(timeout=1.0))

    assert packet_id == 0xA7
    assert data == good_packet
    assert len(conn._recv_buffer) == 0


def test_malformed_varlen_alone_does_not_hang():
    """A lone malformed var-len frame is discarded, then EOF surfaces cleanly.

    Before the fix this looped forever re-reading the same un-consumable bytes;
    with the fix the header is dropped and the empty stream raises promptly.
    """
    conn = _make_conn(bytes([0xA8, 0x00, 0x01]))  # total_len = 1, impossible

    with pytest.raises(ConnectionError):
        asyncio.run(conn._recv_game_packet(timeout=1.0))

    # The bogus 3-byte header was consumed before giving up on the socket.
    assert len(conn._recv_buffer) == 0


def test_wellformed_varlen_frame_still_parses():
    """Regression guard: a valid variable-length frame is unaffected."""
    # 0xA8 var-len, total_len = 6: header(3) + 3 payload bytes.
    packet = bytes([0xA8, 0x00, 0x06, 0xAA, 0xBB, 0xCC])
    conn = _make_conn(packet)

    packet_id, data = asyncio.run(conn._recv_game_packet(timeout=1.0))

    assert packet_id == 0xA8
    assert data == packet
    assert len(conn._recv_buffer) == 0
