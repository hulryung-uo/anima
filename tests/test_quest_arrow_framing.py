"""Regression guard for the 0xBA QuestArrow fixed length (High Seas / CV_7090+).

The client reports version 7.0.102.3 (>= 7.0.9.0), so ServUO negotiates the
HighSeas protocol and emits QuestArrow as the 10-byte SetArrowHS/CancelArrowHS
packet (``[ID][bool][x:u16][y:u16][serial:u32]``), not the legacy 6-byte form.
If PACKET_LENGTHS[0xBA] is wrong (6), the framer consumes only 6 bytes, leaving
the 4-byte serial in the buffer where it is misread as the next packet's id —
desyncing the entire decompressed game stream.
"""

import asyncio
import struct

import pytest

from anima.client.connection import UoConnection
from anima.client.packets import get_packet_length


class _NoData:
    """StreamReader stand-in that yields EOF, so a hang surfaces as an error."""

    async def read(self, _n: int) -> bytes:
        return b""


def _make_conn(recv_buffer: bytes) -> UoConnection:
    conn = UoConnection(timeout=1.0)
    conn._game_mode = True
    conn._reader = _NoData()  # type: ignore[assignment]
    conn._recv_buffer.extend(recv_buffer)
    return conn


def test_quest_arrow_is_ten_bytes():
    """QuestArrow must be the 10-byte HighSeas variant for our reported version."""
    assert get_packet_length(0xBA) == 10


def test_quest_arrow_does_not_desync_following_packet():
    """A 10-byte 0xBA frame is consumed whole; the next packet parses cleanly.

    The HS QuestArrow carries a trailing u32 serial. With the wrong length (6)
    those 4 serial bytes (here 0x00000000) would be left in the buffer; 0x00 is
    a fixed-length-104 packet, so the loop would block on the socket and EOF
    into a ConnectionError instead of returning the following 0xA7 packet.
    """
    # SetArrowHS: id, display=1, x=100, y=200, serial=0 -> 10 bytes.
    quest_arrow = struct.pack(">BBHHI", 0xBA, 1, 100, 200, 0)
    assert len(quest_arrow) == 10
    next_packet = bytes([0xA7, 0x11, 0x22, 0x33])  # 0xA7 is fixed length 4
    conn = _make_conn(quest_arrow + next_packet)

    pid1, data1 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid1 == 0xBA
    assert data1 == quest_arrow

    pid2, data2 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid2 == 0xA7
    assert data2 == next_packet
    assert len(conn._recv_buffer) == 0


def test_quest_arrow_alone_consumes_exactly_ten_bytes():
    """A lone QuestArrow leaves the buffer empty (no leftover serial bytes)."""
    quest_arrow = struct.pack(">BBHHi", 0xBA, 0, 100, 200, -1)  # CancelArrowHS
    conn = _make_conn(quest_arrow)

    pid, data = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid == 0xBA
    assert data == quest_arrow
    assert len(conn._recv_buffer) == 0
