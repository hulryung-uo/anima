"""Tests for the 0x6F SecureTrade outgoing builders.

Layout verified against ClassicUO's outgoing packets
(src/ClassicUO.Client/Network/OutgoingPackets.cs):

  * ``Send_TradeResponse`` code==1  -> 0x6F | len | 0x01 | serial(u32)
  * ``Send_TradeResponse`` code==2  -> 0x6F | len | 0x02 | serial(u32) | state(u32)
  * ``Send_TradeUpdateGold``        -> 0x6F | len | 0x03 | serial(u32) | gold(u32) | plat(u32)

0x6F is a variable-length packet (PACKET_LENGTHS[0x6F] == 0), so bytes 1-2
are the BE u16 total length.
"""

from __future__ import annotations

import struct

import pytest

from anima.client.packets import (
    PACKET_LENGTHS,
    build_trade_accept,
    build_trade_cancel,
    build_trade_update_gold,
)


def _header(data: bytes) -> tuple[int, int, int]:
    pid = data[0]
    length = (data[1] << 8) | data[2]
    action = data[3]
    return pid, length, action


def test_0x6f_is_variable_length() -> None:
    # The builders rely on bytes 1-2 carrying the total length.
    assert PACKET_LENGTHS[0x6F] == 0


def test_trade_cancel_layout() -> None:
    data = build_trade_cancel(0x12345678)
    pid, length, action = _header(data)
    assert pid == 0x6F
    assert action == 0x01
    assert length == len(data) == 8  # id + len(2) + action + serial(4)
    (serial,) = struct.unpack_from(">I", data, 4)
    assert serial == 0x12345678


def test_trade_cancel_exact_bytes() -> None:
    assert build_trade_cancel(0x0000ABCD) == b"\x6f\x00\x08\x01\x00\x00\xab\xcd"


@pytest.mark.parametrize("accept", [True, False])
def test_trade_accept_layout(accept: bool) -> None:
    data = build_trade_accept(0xDEADBEEF, accept)
    pid, length, action = _header(data)
    assert pid == 0x6F
    assert action == 0x02
    # id + len(2) + action + serial(4) + state(4) == 12 bytes.
    assert length == len(data) == 12
    serial, state = struct.unpack_from(">II", data, 4)
    assert serial == 0xDEADBEEF
    # State MUST be a 4-byte field (0 or 1), not a single byte. A 1-byte
    # bool here would make the packet 9 bytes long and desync the server's
    # 4-byte read, leaving the trade stuck un-accepted.
    assert state == (1 if accept else 0)


def test_trade_accept_exact_bytes() -> None:
    # accept=True over serial 0x00000001:
    assert build_trade_accept(0x00000001, True) == (
        b"\x6f\x00\x0c\x02\x00\x00\x00\x01\x00\x00\x00\x01"
    )
    # accept=False flips only the final u32.
    assert build_trade_accept(0x00000001, False) == (
        b"\x6f\x00\x0c\x02\x00\x00\x00\x01\x00\x00\x00\x00"
    )


def test_trade_update_gold_layout() -> None:
    data = build_trade_update_gold(0x0BADF00D, gold=1500, platinum=3)
    pid, length, action = _header(data)
    assert pid == 0x6F
    assert action == 0x03
    # id + len(2) + action + serial(4) + gold(4) + plat(4) == 16 bytes.
    assert length == len(data) == 16
    serial, gold, plat = struct.unpack_from(">III", data, 4)
    assert serial == 0x0BADF00D
    assert gold == 1500
    assert plat == 3


def test_trade_update_gold_default_platinum_zero() -> None:
    data = build_trade_update_gold(0x00000002, gold=42)
    _, _, plat = struct.unpack_from(">III", data, 4)
    assert plat == 0


def test_gold_overflow_is_clamped_not_wrapped() -> None:
    # A computed amount exceeding u32 must clamp to the max, never wrap to a
    # smaller valid-looking offer (and never raise struct.error).
    data = build_trade_update_gold(0x00000003, gold=0xFFFFFFFF + 100)
    _, gold, _ = struct.unpack_from(">III", data, 4)
    assert gold == 0xFFFFFFFF


def test_serial_high_bit_does_not_raise() -> None:
    # Trade serials routinely have the high bit set; the builder must mask to
    # u32 rather than overflow struct.pack(">I", ...).
    for builder in (
        lambda: build_trade_cancel(0xFFFFFFFF),
        lambda: build_trade_accept(0xFFFFFFFF, True),
        lambda: build_trade_update_gold(0xFFFFFFFF, 0),
    ):
        data = builder()
        (serial,) = struct.unpack_from(">I", data, 4)
        assert serial == 0xFFFFFFFF
