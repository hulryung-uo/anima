"""cancel_target must emit ServUO's escape-cancel form (x=y=0xFFFF, serial=0).

ServUO recognises a target-cursor cancel only via the "User pressed escape"
branch (Server/Network/PacketHandlers.cs:1267):
    ``if (x == -1 && y == -1 && !serial.IsValid) t.Cancel(...)``
x/y are read as signed Int16, so -1 on the wire is 0xFFFF. That check runs
BEFORE ``Target.TargetIDValidation`` (line 1272), so the escape form clears the
cursor even when the echoed cursor_id is stale. A zero-coordinate response only
cancels by falling through the invalid-serial branch, which sits behind the
TargetID check and silently no-ops on a stale id. Mirrors ClassicUO
``Send_TargetCancel`` (OutgoingPackets.cs:1781).
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
async def test_cancel_target_sends_escape_form():
    from anima.actions.target import cancel_target

    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    ctx.perception.self_state.pending_target = {"cursor_id": 0x11111111}

    result = await cancel_target(ctx)
    assert result.success
    # Pending cursor is cleared locally too.
    assert ctx.perception.self_state.pending_target is None

    ctx.conn.send_packet.assert_called_once()
    pkt = ctx.conn.send_packet.call_args[0][0]
    fields = _decode_6c(pkt)

    # ServUO escape branch: x == -1 && y == -1 && !serial.IsValid.
    # On the wire that is x = y = 0xFFFF and serial = 0.
    assert fields["x"] == 0xFFFF, "x must be 0xFFFF (-1) to hit the escape branch"
    assert fields["y"] == 0xFFFF, "y must be 0xFFFF (-1) to hit the escape branch"
    assert fields["serial"] == 0, "serial must be 0 so !serial.IsValid holds"
    # The cursor id still echoes the pending cursor.
    assert fields["cursor_id"] == 0x11111111


@pytest.mark.asyncio
async def test_cancel_target_noop_without_pending():
    from anima.actions.target import cancel_target

    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    ctx.perception.self_state.pending_target = None

    result = await cancel_target(ctx)
    assert result.success
    ctx.conn.send_packet.assert_not_called()
