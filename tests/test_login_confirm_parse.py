"""LoginConfirm (0x1B) field-alignment regression tests.

ServUO ``LoginConfirm`` (Packets.cs:4542) writes Z as a 16-bit short and the
direction byte immediately after it; ClassicUO ``EnterWorld``
(PacketHandlers.cs:883) reads ``(sbyte)ReadUInt16BE()`` for Z then the next
byte ``& 0x7`` for direction. The old inline parser stepped over the real
direction byte and read ServUO's trailing constant 0, pinning the login
facing to North for every character.
"""

from __future__ import annotations

import struct

from anima.client.connection import parse_login_confirm


def _build_login_confirm(serial: int, body: int, x: int, y: int, z: int, direction: int) -> bytes:
    """Build a ServUO-faithful 0x1B LoginConfirm packet (fixed 37 bytes)."""
    buf = bytearray()
    buf += struct.pack(">B", 0x1B)
    buf += struct.pack(">I", serial)
    buf += struct.pack(">I", 0)            # unknown / always 0
    buf += struct.pack(">h", body)         # (short)Body
    buf += struct.pack(">h", x)            # (short)X
    buf += struct.pack(">h", y)            # (short)Y
    buf += struct.pack(">h", z)            # (short)Z  <-- 16-bit field
    buf += struct.pack(">B", direction)    # (byte)Direction
    buf += struct.pack(">B", 0)            # (byte)0
    buf += struct.pack(">i", -1)           # -1
    buf += struct.pack(">h", 0)            # map x
    buf += struct.pack(">h", 0)            # map y
    buf += struct.pack(">h", 6144)         # map width
    buf += struct.pack(">h", 4096)         # map height
    # Server Fill()s the rest with zeros to the fixed 37-byte length.
    buf += b"\x00" * (37 - len(buf))
    assert len(buf) == 37
    return bytes(buf)


def test_direction_is_read_not_pinned_to_north() -> None:
    # The bug: direction was read from the trailing constant 0 byte, so any
    # non-North facing decoded as 0. Every value 1..7 must round-trip.
    for direction in range(8):
        pkt = _build_login_confirm(
            serial=0x0BEEF, body=0x0190, x=1000, y=2000, z=0, direction=direction
        )
        result = parse_login_confirm(pkt)
        assert result.direction == direction, f"facing {direction} decoded as {result.direction}"


def test_direction_high_bits_masked() -> None:
    # Direction byte often carries the run flag (0x80) in other packets; the
    # login facing must be masked to the low 3 bits like ClassicUO does.
    pkt = _build_login_confirm(
        serial=1, body=0x0190, x=10, y=20, z=0, direction=0x84
    )
    assert parse_login_confirm(pkt).direction == 0x04


def test_negative_z_decodes_as_signed_byte() -> None:
    # ServUO writes (short)Z; ClassicUO narrows via (sbyte). A negative Z must
    # come back negative, and the following direction byte must stay aligned.
    pkt = _build_login_confirm(
        serial=1, body=0x0190, x=10, y=20, z=-5, direction=3
    )
    result = parse_login_confirm(pkt)
    assert result.z == -5
    assert result.direction == 3


def test_core_fields_unchanged() -> None:
    pkt = _build_login_confirm(
        serial=0x12345678, body=0x0191, x=1234, y=5678, z=12, direction=2
    )
    result = parse_login_confirm(pkt)
    assert result.serial == 0x12345678
    assert result.body == 0x0191
    assert result.x == 1234
    assert result.y == 5678
    assert result.z == 12
    assert result.direction == 2
