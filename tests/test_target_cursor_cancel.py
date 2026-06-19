"""Tests for the 0x6C TargetCursor *cancel* path in the perception handler.

Verified against ClassicUO's TargetManager.SetTargeting
(src/ClassicUO.Client/Game/Managers/TargetManager.cs):
``IsTargeting = cursorType < TargetType.Cancel`` where the TargetType enum is
``Neutral=0, Harmful=1, Beneficial=2, Cancel=3``. A 0x6C whose flag byte is 3
is the server withdrawing the cursor, so the client must NOT keep it around as
something to answer.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.walker import WalkerManager
from anima.perception.handlers import register_handlers


def _make_stack():
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _build_6c(target_type: int, cursor_id: int, cursor_flag: int) -> bytes:
    # [0x6C][type:u8][cursorID:u32][flag:u8][serial:u32][x:u16][y:u16][z:u16][graphic:u16]
    return struct.pack(
        ">BBIBIHHHH",
        0x6C,
        target_type,
        cursor_id,
        cursor_flag,
        0,  # serial
        0,  # x
        0,  # y
        0,  # z
        0,  # graphic
    )


def test_target_request_sets_pending_target():
    h, p, _ = _make_stack()
    h.dispatch(0x6C, _build_6c(target_type=0, cursor_id=0xDEADBEEF, cursor_flag=1))
    assert p.self_state.pending_target is not None
    assert p.self_state.pending_target["cursor_id"] == 0xDEADBEEF
    assert p.self_state.pending_target["cursor_flag"] == 1


def test_server_cancel_does_not_set_pending_target():
    h, p, _ = _make_stack()
    # cursor_flag == 3 (TargetType.Cancel): server withdrawing the cursor.
    h.dispatch(0x6C, _build_6c(target_type=0, cursor_id=0x00000007, cursor_flag=3))
    assert p.self_state.pending_target is None


def test_server_cancel_clears_a_live_pending_target():
    h, p, _ = _make_stack()
    # First a real request lands and is recorded...
    h.dispatch(0x6C, _build_6c(target_type=0, cursor_id=0x11111111, cursor_flag=1))
    assert p.self_state.pending_target is not None
    # ...then the server cancels it. The phantom must be cleared, otherwise it
    # would falsely satisfy the next wait_for_target with a dead cursor_id.
    h.dispatch(0x6C, _build_6c(target_type=0, cursor_id=0x11111111, cursor_flag=3))
    assert p.self_state.pending_target is None
