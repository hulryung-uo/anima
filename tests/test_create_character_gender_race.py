"""The 0xF8 CreateCharacter gender/race byte must match the modern client.

We frame the 0xF8 CreateCharacter70160 packet (ServUO dispatches it only for
CV_70160+ clients, which is the 7.0.102.3 version we advertise). ClassicUO's
``Send_CreateCharacter`` writes this byte as ``race * 2 + female`` with NO
``race--`` for CV >= 7000, so a Human (Race id 1) is 2 (male) / 3 (female).
The previous builder sent the legacy pre-7.0 value 0 / 1.
"""

from __future__ import annotations

from anima.client.appearance import CharacterAppearance, build_create_character

# Byte offset of the gender/race field inside the 0xF8 frame:
#   ID(1) + pattern1(4) + pattern2(4) + pattern3(1) + name(30) + unk(2)
#   + clientFlags(4) + unk(4) + loginCount(4) + profession(1) + reserved(15)
# = 70
_GENDER_RACE_OFFSET = 70
_HUMAN_RACE_ID = 1


def _decode_servuo_stygian(gender_race: int) -> tuple[int, bool]:
    """Replicate ServUO's CreateCharacter StygianAbyss decode.

    PacketHandlers.cs CreateCharacter:
        female = (genderRace % 2) != 0
        raceID = genderRace < 4 ? 0 : (genderRace / 2) - 1   # 0 = Human
    """
    female = (gender_race % 2) != 0
    race_id = 0 if gender_race < 4 else (gender_race // 2) - 1
    return race_id, female


def test_male_human_gender_race_byte_is_2() -> None:
    pkt = build_create_character(CharacterAppearance(name="Anima", female=False))
    assert pkt[0] == 0xF8
    assert pkt[_GENDER_RACE_OFFSET] == _HUMAN_RACE_ID * 2  # 2


def test_female_human_gender_race_byte_is_3() -> None:
    pkt = build_create_character(CharacterAppearance(name="Anima", female=True))
    assert pkt[_GENDER_RACE_OFFSET] == _HUMAN_RACE_ID * 2 + 1  # 3


def test_not_legacy_pre_7000_encoding() -> None:
    # Guard against regressing to the legacy ``(race-1)*2 + female`` = 0/1
    # byte that a CV_70160 client never sends.
    male = build_create_character(CharacterAppearance(female=False))
    female = build_create_character(CharacterAppearance(female=True))
    assert male[_GENDER_RACE_OFFSET] not in (0, 1)
    assert female[_GENDER_RACE_OFFSET] not in (0, 1)


def test_servuo_decode_round_trips_to_human() -> None:
    # The byte we emit must decode (under ServUO's StygianAbyss path) to
    # Human race with the correct gender.
    for female in (False, True):
        pkt = build_create_character(CharacterAppearance(female=female))
        race_id, decoded_female = _decode_servuo_stygian(pkt[_GENDER_RACE_OFFSET])
        assert race_id == 0, "must decode to Human (race id 0)"
        assert decoded_female is female


def test_packet_length_unchanged() -> None:
    pkt = build_create_character(CharacterAppearance())
    assert len(pkt) == 106
