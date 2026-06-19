"""Tests for the 0x83 DeleteCharacter outgoing builder.

Verified against ClassicUO ``Send_DeleteCharacter``
(src/ClassicUO.Client/Network/OutgoingPackets.cs) and ServUO's
``PacketHandlers.DeleteCharacter``:

    [0x83][30 zero bytes][slot:u32 BE][clientIP:u32 BE]   (fixed 39 bytes)

The 30-byte field is reserved/zeroed on modern clients — it is NOT the
account password. ServUO ``Seek(30, SeekOrigin.Current)`` past it. Writing
the cleartext password there diverges from the reference frame and leaks the
secret to anything that reads those bytes.
"""

from __future__ import annotations

import struct

from anima.client.packets import PACKET_LENGTHS, build_delete_character


def test_delete_character_fixed_length() -> None:
    data = build_delete_character("hunter2", slot=0)
    assert len(data) == 39
    assert PACKET_LENGTHS[0x83] == 39


def test_delete_character_layout() -> None:
    data = build_delete_character("hunter2", slot=3, client_ip=0x7F000001)
    assert data[0] == 0x83
    # Bytes 1..31 are the reserved 30-byte field — must be all zeros.
    assert data[1:31] == b"\x00" * 30
    (slot,) = struct.unpack_from(">I", data, 31)
    (client_ip,) = struct.unpack_from(">I", data, 35)
    assert slot == 3
    assert client_ip == 0x7F000001


def test_password_never_appears_on_the_wire() -> None:
    # The cleartext password must not leak into the packet bytes.
    secret = "s3cr3t-passphrase"
    data = build_delete_character(secret, slot=1)
    assert secret.encode("ascii") not in data
    # And the reserved field stays zeroed regardless of password content.
    assert data[1:31] == b"\x00" * 30


def test_slot_is_big_endian_after_reserved_field() -> None:
    # A non-trivial slot must land in the u32 BE immediately after the
    # 30 zero bytes (offset 31), not be shifted by a stray password write.
    data = build_delete_character("", slot=0x01020304)
    assert data[31:35] == b"\x01\x02\x03\x04"
