"""SupportedFeatures (0xB9) flag-width regression tests.

We advertise a modern client (7.0.102.3 >= CV_60142), so ServUO sends the
*extended* 5-byte frame ``[0xB9][flags:u32]`` (Packets.cs ``SupportedFeatures``:
``base(0xB9, ExtendedSupportedFeatures ? 5 : 3)`` then ``Write((uint)flags)``)
and ClassicUO frames it as 5 bytes (``_packetsTable[0xB9] = 0x05``). The old
handler used ``read_u32() if len(data) > 5 else read_u16()``; the framer always
delivers exactly 5 bytes, so it always read a u16 and dropped the high 16 bits
of the 32-bit feature flags.
"""

from __future__ import annotations

import struct

from anima.client.connection import parse_supported_features


def _extended(flags: int) -> bytes:
    """ServUO extended 0xB9 frame: [0xB9][flags:u32 BE] (5 bytes)."""
    return struct.pack(">B", 0xB9) + struct.pack(">I", flags)


def _legacy(flags: int) -> bytes:
    """Pre-6.0.14.2 legacy 0xB9 frame: [0xB9][flags:u16 BE] (3 bytes)."""
    return struct.pack(">B", 0xB9) + struct.pack(">H", flags)


def test_extended_frame_reads_full_u32() -> None:
    # A flags value with bits set in BOTH halves of the 32-bit field. The bug
    # truncated to the low 16 bits; the high bits (real capability flags such as
    # SeventhCharacterSlot / LiveAccount) must survive.
    flags = 0x800F_001F
    assert parse_supported_features(_extended(flags)) == flags


def test_high_bits_not_truncated() -> None:
    # Only the upper 16 bits set: the old u16 read would have returned 0.
    flags = 0xABCD_0000
    assert parse_supported_features(_extended(flags)) == flags
    assert parse_supported_features(_extended(flags)) != (flags & 0xFFFF)


def test_legacy_three_byte_frame_reads_u16() -> None:
    # Pre-CV_60142 clients still get the 3-byte frame; read it as a u16.
    flags = 0x9CD3
    assert parse_supported_features(_legacy(flags)) == flags


def test_low_bits_only_round_trip() -> None:
    flags = 0x0000_7FFF
    assert parse_supported_features(_extended(flags)) == flags
