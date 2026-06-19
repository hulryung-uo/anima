"""The 0xF8 CreateCharacter client-flags field must advertise the facets.

ServUO ``PacketHandlers.CreateCharacter70160`` reads a 4-byte client-flags
field and stores ``state.Flags = (ClientFlags)flags``. ClassicUO writes its
negotiated ``Protocol`` value there; a bare 0 makes the server believe the
client supports no expansions/facets (``ClientFlags.None``) at creation time.
This mirrors the regression already fixed for 0x5D ``build_play_character``.

Field offset inside the 0xF8 frame:
  ID(1) + pattern1(4) + pattern2(4) + pattern3(1) + name(30) + unk(2) = 42
"""

from __future__ import annotations

import struct

from anima.client.appearance import CharacterAppearance, build_create_character

_CLIENT_FLAGS_OFFSET = 42
# Re-stated here so the test pins the wire value independently of the source
# constant: Fel|Tram|Ilsh|Malas|Tokuno|TerMur.
_ALL_FACETS = 0x3F
# The gender/race byte offset (see test_create_character_gender_race.py); used
# to prove the new value did not shift any following field.
_GENDER_RACE_OFFSET = 70


def _client_flags(pkt: bytes) -> int:
    (flags,) = struct.unpack_from(">I", pkt, _CLIENT_FLAGS_OFFSET)
    return flags


def test_create_character_defaults_to_full_facet_flags() -> None:
    # Regression: the field used to be hard-coded to 0 (ClientFlags.None),
    # so ServUO recorded a client that claimed support for no facets.
    pkt = build_create_character(CharacterAppearance(name="Anima"))
    assert pkt[0] == 0xF8
    assert _client_flags(pkt) == _ALL_FACETS


def test_create_character_flags_are_overridable() -> None:
    pkt = build_create_character(
        CharacterAppearance(name="Anima"), client_flags=0x01
    )
    assert _client_flags(pkt) == 0x01


def test_create_character_flags_field_did_not_shift_other_fields() -> None:
    # Writing the flags must not change packet length or the position of the
    # gender/race byte that follows it.
    male = build_create_character(CharacterAppearance(name="Anima", female=False))
    assert len(male) == 106
    assert male[_GENDER_RACE_OFFSET] == 2  # Human male (unchanged)


def test_create_character_pattern_header_unchanged() -> None:
    pkt = build_create_character(CharacterAppearance(name="Anima"))
    assert struct.unpack_from(">I", pkt, 1)[0] == 0xEDEDEDED  # pattern1
    assert struct.unpack_from(">I", pkt, 5)[0] == 0xFFFFFFFF  # pattern2
    # The "unknown=1" word immediately after the flags must be intact.
    assert struct.unpack_from(">I", pkt, _CLIENT_FLAGS_OFFSET + 4)[0] == 0x01
