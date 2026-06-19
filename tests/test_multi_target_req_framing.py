"""Regression guard for the 0x99 MultiTargetReq fixed length (CV_7090+).

The client reports version 7.0.102.3 (>= 7.0.9.0), so ServUO negotiates the
HighSeas protocol and emits MultiTargetReqHS as a FIXED 30-byte packet
(Server/Network/Packets.cs:2746, ``base(0x99, 30)``):
    ``[0x99][AllowGround:bool][TargetID:u32][Flags:u8]...[MultiID/Offsets]``.
ClassicUO mirrors this with ``_packetsTable[0x99] = 0x1E`` (30) for CV_7090+.

If PACKET_LENGTHS[0x99] is declared variable (0), the framer reads bytes 1-2
(the AllowGround bool + the high byte of TargetID) as a big-endian u16 length.
For a typical small TargetID that is < 3, tripping the malformed-frame guard,
which discards only the 3-byte header and reinterprets the remaining 27
packet-body bytes as new packet ids — desyncing the entire game stream.
"""

import asyncio
import struct

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


def _make_multi_target_req(target_id: int = 0x40000001) -> bytes:
    """Build a 30-byte MultiTargetReqHS frame (ServUO layout)."""
    body = bytearray(29)  # 30 total minus the id byte
    body[0] = 1  # AllowGround
    struct.pack_into(">I", body, 1, target_id)  # TargetID
    body[5] = 0  # Flags
    struct.pack_into(">hhhh", body, 17, 0x4000, 0, 0, 0)  # MultiID + offsets
    frame = bytes([0x99]) + bytes(body)
    assert len(frame) == 30
    return frame


def test_multi_target_req_is_thirty_bytes():
    """MultiTargetReq must be the fixed 30-byte HS variant for our version."""
    assert get_packet_length(0x99) == 30


def test_multi_target_req_does_not_desync_following_packet():
    """A 30-byte 0x99 frame is consumed whole; the next packet parses cleanly.

    With the wrong (variable) declaration the framer would read the
    AllowGround/TargetID bytes as a length, discard 3 bytes, and misread the
    rest — so the trailing 0xA7 (fixed length 4) would never be returned.
    """
    multi_req = _make_multi_target_req()
    next_packet = bytes([0xA7, 0x11, 0x22, 0x33])  # 0xA7 is fixed length 4
    conn = _make_conn(multi_req + next_packet)

    pid1, data1 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid1 == 0x99
    assert data1 == multi_req

    pid2, data2 = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid2 == 0xA7
    assert data2 == next_packet
    assert len(conn._recv_buffer) == 0


def test_multi_target_req_alone_consumes_exactly_thirty_bytes():
    """A lone MultiTargetReq leaves the buffer empty (no leftover body bytes)."""
    multi_req = _make_multi_target_req(target_id=0x00000001)
    conn = _make_conn(multi_req)

    pid, data = asyncio.run(conn._recv_game_packet(timeout=1.0))
    assert pid == 0x99
    assert data == multi_req
    assert len(conn._recv_buffer) == 0
