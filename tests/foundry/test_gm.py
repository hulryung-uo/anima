"""GM driver packet builders + JSONL tail reader (offline)."""

from __future__ import annotations

import json
import struct

from foundry.kernel.gm import (
    JsonlWatch,
    build_account_login,
    build_client_version,
    build_create_character,
    build_game_login,
    build_play_character,
    build_seed,
    build_server_select,
    build_target_response,
    build_unicode_speech,
)


def test_fixed_packet_sizes():
    assert len(build_seed(1)) == 21
    assert len(build_account_login("u", "p")) == 62
    assert len(build_server_select(0)) == 3
    assert len(build_game_login(0xDEADBEEF, "u", "p")) == 65
    assert len(build_play_character("FoundryGM")) == 73
    assert len(build_create_character("FoundryGM")) == 106
    assert len(build_target_response(7, 0x1AF)) == 19


def test_seed_carries_version():
    d = build_seed(0xCAFEBABE)
    assert d[0] == 0xEF
    assert struct.unpack_from(">I", d, 1)[0] == 0xCAFEBABE
    assert struct.unpack_from(">IIII", d, 5) == (7, 0, 102, 3)


def test_unicode_speech_plain_mode():
    pkt = build_unicode_speech("[Go 2563 491")
    assert pkt[0] == 0xAD
    assert struct.unpack_from(">H", pkt, 1)[0] == len(pkt)
    assert pkt[3] == 0x00  # plain type, no keyword flag
    assert pkt[8:12] == b"ENU\x00"
    text = pkt[12:-2].decode("utf-16-be")
    assert text == "[Go 2563 491"
    assert pkt[-2:] == b"\x00\x00"


def test_client_version_length_prefix():
    pkt = build_client_version("7.0.102.3")
    assert pkt[0] == 0xBD
    assert struct.unpack_from(">H", pkt, 1)[0] == len(pkt)
    assert pkt[3:].rstrip(b"\x00") == b"7.0.102.3"


def test_target_response_fields():
    pkt = build_target_response(cursor_id=0x11223344, serial=0x000001AF)
    assert pkt[0] == 0x6C
    assert pkt[1] == 0x00  # object target
    assert struct.unpack_from(">I", pkt, 2)[0] == 0x11223344
    assert struct.unpack_from(">I", pkt, 7)[0] == 0x000001AF


def test_create_character_core_fields():
    pkt = build_create_character("FoundryGM", city_index=3)
    assert pkt[10:40].split(b"\x00", 1)[0] == b"FoundryGM"
    # stats at 71..73, skill pairs at 74..81
    assert tuple(pkt[71:74]) == (60, 10, 10)
    assert tuple(pkt[74:78]) == (45, 50, 7, 50)
    assert struct.unpack_from(">H", pkt, 92)[0] == 3  # city index


def test_jsonl_watch_incremental(tmp_path):
    f = tmp_path / "t.jsonl"
    w = JsonlWatch(f)
    assert w.poll() == []
    f.write_text(json.dumps({"a": 1}) + "\n" + '{"partial": ')
    evs = w.poll()
    assert evs == [{"a": 1}]
    # completing the partial line makes it visible on the next poll
    with open(f, "a") as fh:
        fh.write('2}\n')
    assert w.poll() == [{"partial": 2}]
    assert w.poll() == []


def test_lane_spots_are_separated():
    """Lanes must stay outside speech (~18) and wipe (±12) interference."""
    import itertools

    from foundry.kernel.gm import LANE_SPOTS
    assert len(LANE_SPOTS) >= 5
    for a, b in itertools.combinations(LANE_SPOTS, 2):
        assert max(abs(a[0] - b[0]), abs(a[1] - b[1])) >= 32, (a, b)
