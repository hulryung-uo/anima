"""Regression guard: ServUO-emitted fixed packets must frame as whole units.

ServUO sends several FIXED-length server->client packets that were missing
from PACKET_LENGTHS and so defaulted to -1 (unknown). The game framer
(``UoConnection._recv_game_packet``) handles an unknown packet id by discarding
exactly ONE byte and reinterpreting the next byte as a fresh packet id
(connection.py: ``del self._recv_buffer[:1]``). For any multi-byte packet that
misframes the entire decompressed game stream from that point on — the same
desync class already fixed for 0x99 (MultiTargetReq) and 0xBA (QuestArrow).

The affected ServUO packets and their fixed lengths (ClassicUO PacketsTable.cs):
    0x53 PopupMessage      -> 2   (Server/Network/Packets.cs:3156 base(0x53, 2))
    0x7B Sequence          -> 2   (Server/Network/Packets.cs:2560 base(0x7B, 2))
    0xC6 InvalidMapEnable  -> 1   (Server/Network/Packets.cs:777  base(0xC6, 1))
    0xC9 TripTimeResponse  -> 6   (Server/Network/Packets.cs:650  base(0xC9, 6))
"""

import asyncio

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


@pytest.mark.parametrize(
    ("packet_id", "length"),
    [
        (0x53, 2),
        (0x7B, 2),
        (0xC6, 1),
        (0xC9, 6),
    ],
)
def test_fixed_length_declared(packet_id: int, length: int):
    """Each ServUO-emitted packet must carry its exact fixed length, not -1."""
    assert get_packet_length(packet_id) == length


@pytest.mark.parametrize(
    ("packet_id", "length"),
    [
        (0x53, 2),
        (0x7B, 2),
        (0xC6, 1),
        (0xC9, 6),
    ],
)
def test_does_not_desync_following_packet(packet_id: int, length: int):
    """A fixed frame is consumed whole; the trailing packet parses cleanly.

    With the wrong (unknown/-1) declaration the framer would discard a single
    byte and reinterpret the rest, so the trailing 0xA7 (fixed length 4) would
    never be returned intact.
    """
    # Build the leading frame: id byte + (length - 1) deterministic body bytes.
    frame = bytes([packet_id]) + bytes(range(1, length))
    assert len(frame) == length
    next_packet = bytes([0xA7, 0x11, 0x22, 0x33])  # 0xA7 is fixed length 4
    conn = _make_conn(frame + next_packet)

    pid1, data1 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid1 == packet_id
    assert data1 == frame

    pid2, data2 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid2 == 0xA7
    assert data2 == next_packet
    assert len(conn._recv_buffer) == 0


@pytest.mark.parametrize(
    ("packet_id", "length"),
    [
        (0x53, 2),
        (0x7B, 2),
        (0xC6, 1),
        (0xC9, 6),
    ],
)
def test_alone_consumes_exactly_its_length(packet_id: int, length: int):
    """A lone frame leaves the buffer empty (no leftover body bytes)."""
    frame = bytes([packet_id]) + bytes(range(1, length))
    conn = _make_conn(frame)

    pid, data = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid == packet_id
    assert data == frame
    assert len(conn._recv_buffer) == 0
