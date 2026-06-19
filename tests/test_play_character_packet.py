"""Tests for the 0x5D PlayCharacter (SelectCharacter) builder.

Verified against ClassicUO ``Send_SelectCharacter``
(src/ClassicUO.Client/Network/OutgoingPackets.cs) and ServUO
``PacketHandlers.PlayCharacter`` (Server/Network/PacketHandlers.cs):

  * Fixed 73-byte packet: [0x5D][pattern 0xEDEDEDED u32]
    [name ASCII 30][zero 2][client-flags u32][zero 24][slot u32][ip u32].
  * ServUO reads the client-flags u32 at offset 36 and stores it as
    ``state.Flags = (ClientFlags)flags``. ClassicUO writes its negotiated
    ``Protocol`` value there; a bare 0 makes the server believe the client
    supports no expansions/facets (``ClientFlags.None``).
"""

from __future__ import annotations

import struct

from anima.client.packets import build_play_character


def _flags_field(pkt: bytes) -> int:
    # offset: 1 (id) + 4 (pattern) + 30 (name) + 2 (unknown) = 37
    (flags,) = struct.unpack_from(">I", pkt, 37)
    return flags


def test_play_character_is_fixed_73_bytes() -> None:
    pkt = build_play_character(name="Anima", slot=0)
    assert len(pkt) == 73
    assert pkt[0] == 0x5D
    assert struct.unpack_from(">I", pkt, 1)[0] == 0xEDEDEDED


def test_play_character_defaults_to_full_facet_flags() -> None:
    # Regression: the field used to be hard-coded to 0 (ClientFlags.None),
    # so ServUO recorded a client that claimed support for no facets.
    pkt = build_play_character(name="Anima", slot=0)
    assert _flags_field(pkt) == 0x3F  # Fel|Tram|Ilsh|Malas|Tokuno|TerMur


def test_play_character_flags_are_overridable() -> None:
    pkt = build_play_character(name="Anima", slot=0, client_flags=0x01)
    assert _flags_field(pkt) == 0x01


def test_play_character_slot_and_ip_follow_the_flags() -> None:
    pkt = build_play_character(name="Anima", slot=3, client_ip=0x7F000001)
    # slot u32 at offset 1+4+30+2+4+24 = 65, ip u32 at 69
    assert struct.unpack_from(">I", pkt, 65)[0] == 3
    assert struct.unpack_from(">I", pkt, 69)[0] == 0x7F000001


def test_play_character_name_is_null_padded() -> None:
    pkt = build_play_character(name="Anima", slot=0)
    name_field = pkt[5:35]
    assert name_field[:5] == b"Anima"
    assert name_field[5:] == b"\x00" * 25
