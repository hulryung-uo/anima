"""Tests for the 0x6C target cursor response builder.

Verified against ClassicUO Send_TargetObject / Send_TargetXYZ
(src/ClassicUO.Client/Network/OutgoingPackets.cs): the byte after the
u32 cursor ID is the cursor *flag/type*, which must echo the value the
server sent in its 0x6C target request.
"""

import struct

from anima.client.packets import build_target_response


def _unpack_6c(data: bytes):
    assert len(data) == 19
    (
        pid,
        target_type,
        cursor_id,
        cursor_flag,
        serial,
        x,
        y,
        z,
        graphic,
    ) = struct.unpack(">BBIBIHHHH", data)
    return {
        "pid": pid,
        "target_type": target_type,
        "cursor_id": cursor_id,
        "cursor_flag": cursor_flag,
        "serial": serial,
        "x": x,
        "y": y,
        "z": z,
        "graphic": graphic,
    }


def test_target_response_layout_and_length():
    data = build_target_response(
        target_type=0,
        cursor_id=0xDEADBEEF,
        serial=0x40001234,
        cursor_flag=1,
    )
    p = _unpack_6c(data)
    assert p["pid"] == 0x6C
    assert p["target_type"] == 0
    assert p["cursor_id"] == 0xDEADBEEF
    # The byte after the cursor id is the echoed flag, NOT a hardcoded 0.
    assert p["cursor_flag"] == 1
    assert p["serial"] == 0x40001234


def test_target_response_echoes_harmful_flag():
    # A combat target request arrives with the "harmful" cursor flag (1);
    # the response must echo it back at byte offset 6.
    data = build_target_response(
        target_type=0,
        cursor_id=0x00000007,
        serial=0x12345678,
        cursor_flag=1,
    )
    assert data[6] == 1


def test_target_response_default_flag_is_neutral():
    # Backward compatibility: callers that omit the flag get neutral (0).
    data = build_target_response(
        target_type=0,
        cursor_id=0x00000007,
        serial=0x12345678,
    )
    assert data[6] == 0


def test_target_response_tile_carries_flag_and_coords():
    data = build_target_response(
        target_type=1,
        cursor_id=0x11223344,
        x=100,
        y=200,
        z=0xFFFB,  # -5 as unsigned i16
        graphic=0x1234,
        cursor_flag=2,
    )
    p = _unpack_6c(data)
    assert p["target_type"] == 1
    assert p["cursor_flag"] == 2
    assert p["x"] == 100 and p["y"] == 200
    assert p["z"] == 0xFFFB
    assert p["graphic"] == 0x1234
    assert p["serial"] == 0
