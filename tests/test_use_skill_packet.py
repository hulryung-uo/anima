"""Tests for the 0x12 UseSkill (TextCommand sub-type 0x24) builder.

Verified against ClassicUO ``Send_UseSkill``
(src/ClassicUO.Client/Network/OutgoingPackets.cs) and ServUO's
``PacketHandlers.TextCommand`` / ``Skills.UseSkill``:

  * ID byte 0x12, then a u16 BE total-length field (variable packet).
  * sub-type byte 0x24 ("Use skill").
  * ASCII command ``"{idx} 0"`` followed by a single NUL terminator
    (ClassicUO ``WriteASCII`` always appends 0x00).
  * ServUO parses ``command.Split(' ')[0]`` as the skill index and only
    acts when ``0 <= skillID < SkillInfo.Table.Length`` — so a negative
    id is a silently-discarded no-op on the server. ClassicUO's
    ``GameActions.UseSkill`` mirrors this with an ``index >= 0`` guard.
"""

from __future__ import annotations

import pytest

from anima.client.packets import build_use_skill


def _unpack_use_skill(data: bytes) -> tuple[int, int, int, str]:
    pid = data[0]
    length = (data[1] << 8) | data[2]
    subtype = data[3]
    command = data[4:]
    assert command.endswith(b"\x00"), "ASCII command must be NUL-terminated"
    text = command[:-1].decode("ascii")
    return pid, length, subtype, text


@pytest.mark.parametrize("skill_id", [0, 9, 21, 46, 47, 255])
def test_use_skill_layout_and_length(skill_id: int) -> None:
    data = build_use_skill(skill_id)
    pid, length, subtype, text = _unpack_use_skill(data)

    assert pid == 0x12
    assert subtype == 0x24
    # ClassicUO sends "{idx} 0"; the trailing "0" is ignored by ServUO.
    assert text == f"{skill_id} 0"
    # Variable packet: the BE u16 length field is the total byte count.
    assert length == len(data)


def test_use_skill_exact_bytes() -> None:
    # Meditation (ServUO SkillName=46): 0x12 | len=9 | 0x24 | "46 0\0"
    assert build_use_skill(46) == b"\x12\x00\x09\x24" + b"46 0\x00"


def test_server_splits_on_first_space() -> None:
    # ServUO does command.Split(' ')[0] -> the index must be the first
    # space-delimited token, never glued to the trailing argument.
    _, _, _, text = _unpack_use_skill(build_use_skill(21))
    assert text.split(" ")[0] == "21"


@pytest.mark.parametrize("bad_id", [-1, -46, -255])
def test_negative_skill_id_rejected(bad_id: int) -> None:
    # ClassicUO GameActions.UseSkill refuses index < 0; building such a
    # packet would yield a server-discarded no-op that the action layer
    # would still report as a successful "skill used".
    with pytest.raises(ValueError):
        build_use_skill(bad_id)
