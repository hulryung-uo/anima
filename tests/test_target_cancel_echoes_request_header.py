"""cancel_target must echo the original cursor type byte + flag.

ClassicUO's TargetManager.CancelTarget() (TargetManager.cs:210) sends
``Send_TargetCancel(TargetingState, _targetCursorId, (byte)TargetingType)`` —
the cancel reply carries the SAME type byte (CursorTarget) and cursor flag
(harmful/helpful) that the server's 0x6C request raised. The 0x6C handler
stashes ``target_type`` and ``cursor_flag`` into ``pending_target``; the cancel
must replay both, not hardcode them to 0. A flag-validating server can reject a
cancel whose header bytes don't match the request, leaving the cursor open and
wedging the next targeted action. The escape coordinates (x=y=0xFFFF, serial=0)
still drive ServUO's "User pressed escape" branch (PacketHandlers.cs:1267).
"""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import pytest


def _decode_6c(pkt: bytes) -> dict:
    # [0x6C][type:u8][cursorID:u32][flag:u8][serial:u32][x:u16][y:u16][z:u16][graphic:u16]
    assert pkt[0] == 0x6C
    assert len(pkt) == 19
    (
        _id, target_type, cursor_id, flag, serial, x, y, z, graphic,
    ) = struct.unpack(">BBIBIHHHH", pkt)
    return {
        "type": target_type,
        "cursor_id": cursor_id,
        "flag": flag,
        "serial": serial,
        "x": x,
        "y": y,
        "z": z,
        "graphic": graphic,
    }


@pytest.mark.asyncio
async def test_cancel_echoes_ground_type_and_helpful_flag():
    from anima.actions.target import cancel_target

    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    # Server raised a GROUND cursor (type 1) with a HELPFUL flag (2).
    ctx.perception.self_state.pending_target = {
        "target_type": 1,
        "cursor_id": 0xABCDEF01,
        "cursor_flag": 2,
    }

    result = await cancel_target(ctx)
    assert result.success
    assert ctx.perception.self_state.pending_target is None

    ctx.conn.send_packet.assert_called_once()
    fields = _decode_6c(ctx.conn.send_packet.call_args[0][0])

    # Header bytes echo the request, not hardcoded 0.
    assert fields["type"] == 1, "type byte must echo the request's cursor type"
    assert fields["flag"] == 2, "cursor flag must echo the request's flag"
    assert fields["cursor_id"] == 0xABCDEF01
    # Escape coordinates still present so ServUO's escape branch fires.
    assert fields["x"] == 0xFFFF
    assert fields["y"] == 0xFFFF
    assert fields["serial"] == 0


@pytest.mark.asyncio
async def test_cancel_echoes_harmful_object_cursor():
    from anima.actions.target import cancel_target

    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    # Object cursor (type 0) with a HARMFUL flag (1).
    ctx.perception.self_state.pending_target = {
        "target_type": 0,
        "cursor_id": 0x00000042,
        "cursor_flag": 1,
    }

    result = await cancel_target(ctx)
    assert result.success

    fields = _decode_6c(ctx.conn.send_packet.call_args[0][0])
    assert fields["type"] == 0
    assert fields["flag"] == 1
    assert fields["cursor_id"] == 0x00000042
    assert fields["x"] == 0xFFFF and fields["y"] == 0xFFFF


@pytest.mark.asyncio
async def test_cancel_defaults_when_fields_absent():
    from anima.actions.target import cancel_target

    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    # Legacy pending_target that only carried a cursor_id.
    ctx.perception.self_state.pending_target = {"cursor_id": 0x11111111}

    result = await cancel_target(ctx)
    assert result.success

    fields = _decode_6c(ctx.conn.send_packet.call_args[0][0])
    assert fields["type"] == 0
    assert fields["flag"] == 0
    assert fields["cursor_id"] == 0x11111111
    assert fields["x"] == 0xFFFF and fields["y"] == 0xFFFF
