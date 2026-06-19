"""UO packet definitions: length table, outgoing builders, and incoming parsers."""

from __future__ import annotations

from anima.client.codec import PacketWriter

# ---------------------------------------------------------------------------
# Packet length table (based on UO protocol / ClassicUO PacketsTable.cs)
#   > 0 = fixed length (including the 1-byte packet ID)
#   0   = variable length (bytes 1-2 = u16 BE total length)
#   -1  = unknown / unhandled
# ---------------------------------------------------------------------------

PACKET_LENGTHS: dict[int, int] = {
    # Complete packet length table based on ClassicUO PacketsTable.cs
    # >0 = fixed length (including ID byte), 0 = variable (bytes 1-2 = BE u16 length)
    0x00: 104,
    0x01: 5,
    0x02: 7,
    0x03: 0,
    0x04: 2,
    0x05: 5,
    0x06: 5,
    0x07: 7,
    0x08: 15,
    0x09: 5,
    0x0A: 11,
    0x0B: 7,
    0x0C: 0,
    0x0D: 3,
    0x0E: 0,
    0x0F: 61,
    0x10: 0,
    0x11: 0,
    0x12: 0,
    0x13: 10,
    0x14: 6,
    0x15: 9,
    0x16: 0,
    0x17: 0,
    0x18: 0,
    0x19: 0,
    0x1A: 0,
    0x1B: 37,
    0x1C: 0,
    0x1D: 5,
    0x1E: 4,
    0x1F: 8,
    0x20: 19,
    0x21: 8,
    0x22: 3,
    0x23: 26,
    0x24: 9,
    0x25: 21,
    0x26: 0,
    0x27: 2,
    0x28: 5,
    0x29: 1,
    0x2A: 5,
    0x2B: 2,
    0x2C: 2,
    0x2D: 17,
    0x2E: 15,
    0x2F: 10,
    0x30: 5,
    0x31: 1,
    0x32: 2,
    0x33: 0,
    0x34: 10,
    0x35: 0,
    0x36: 0,
    0x37: 8,
    0x38: 7,
    0x39: 0,
    0x3A: 0,
    0x3B: 0,
    0x3C: 0,
    0x3E: 37,
    0x3F: 0,
    0x40: 0,
    0x41: 0,
    0x42: 0,
    0x43: 0,
    0x44: 0,
    0x45: 5,
    0x46: 0,
    0x47: 11,
    0x48: 73,
    0x49: 63,
    0x4E: 6,
    0x4F: 2,
    0x54: 12,
    0x55: 1,
    0x56: 11,
    0x57: 110,
    0x58: 106,
    0x5B: 4,
    0x5D: 73,
    0x65: 4,
    0x66: 0,
    0x6C: 19,
    0x6D: 3,
    0x6E: 14,
    0x6F: 0,
    0x70: 28,
    0x71: 0,
    0x72: 5,
    0x73: 2,
    0x74: 0,
    0x75: 35,
    0x76: 16,
    0x77: 17,
    0x78: 0,
    0x7C: 0,
    0x7D: 13,
    0x80: 62,
    0x82: 2,
    0x83: 39,
    0x85: 2,
    0x86: 0,
    0x88: 66,
    0x89: 0,
    0x8C: 11,
    0x90: 19,
    0x91: 65,
    0x93: 99,
    0x95: 9,
    0x97: 2,
    0x98: 0,
    0x99: 0,
    0x9A: 0,
    0x9B: 258,
    0x9E: 0,
    0x9F: 0,
    0xA0: 3,
    0xA1: 9,
    0xA2: 9,
    0xA3: 9,
    0xA4: 149,
    0xA5: 0,
    0xA6: 0,
    0xA7: 4,
    0xA8: 0,
    0xA9: 0,
    0xAA: 5,
    0xAB: 0,
    0xAD: 0,
    0xAE: 0,
    0xAF: 13,
    0xB0: 0,
    0xB1: 0,
    0xB2: 0,
    0xB5: 64,
    0xB6: 9,
    0xB7: 0,
    0xB8: 0,
    0xB9: 5,
    # 0xBA QuestArrow: 10 bytes for the High Seas (CV_7090+) protocol the
    # client negotiates. We report 7.0.102.3 (>= 7.0.9.0), so ServUO sends the
    # SetArrowHS/CancelArrowHS variant [ID][bool][x:u16][y:u16][serial:u32] = 10
    # bytes, not the legacy 6-byte SetArrow/CancelArrow. Reading only 6 leaves
    # the 4-byte serial in the buffer and desyncs every subsequent packet.
    0xBA: 10,
    0xBB: 9,
    0xBC: 3,
    0xBD: 0,
    0xBE: 0,
    0xBF: 0,
    0xC0: 36,
    0xC1: 0,
    0xC2: 0,
    0xC4: 6,
    0xC7: 49,
    0xC8: 2,
    0xCA: 6,
    0xCB: 7,
    0xCC: 0,
    0xCF: 0,
    0xD0: 0,
    0xD1: 2,
    0xD2: 25,
    0xD3: 0,
    0xD4: 0,
    0xD6: 0,
    0xD7: 0,
    0xD8: 0,
    0xD9: 0,
    0xDB: 0,
    0xDC: 9,
    0xDD: 0,
    0xDE: 0,
    0xDF: 0,
    0xE1: 0,
    0xE2: 10,
    0xE3: 0,
    0xE5: 0,
    0xE6: 5,
    0xEC: 0,
    0xED: 0,
    0xEF: 21,
    0xF0: 0,
    0xF1: 0,
    0xF3: 26,
    0xF4: 0,
    0xF5: 21,
    0xF6: 0,
    0xF7: 0,
    0xF8: 106,
    0xFB: 2,
    0xFD: 2,
}


def get_packet_length(packet_id: int) -> int:
    """Get expected packet length. Returns 0 for variable, -1 for unknown."""
    return PACKET_LENGTHS.get(packet_id, -1)


# ---------------------------------------------------------------------------
# Outgoing packet builders
# ---------------------------------------------------------------------------


def build_seed(seed: int, major: int = 7, minor: int = 0, rev: int = 102, patch: int = 3) -> bytes:
    """Build Seed packet (0xEF, 21 bytes)."""
    w = PacketWriter()
    w.write_u8(0xEF)
    w.write_u32(seed)
    w.write_u32(major)
    w.write_u32(minor)
    w.write_u32(rev)
    w.write_u32(patch)
    return w.to_bytes()


def build_game_seed(seed: int) -> bytes:
    """Build the phase-2 game-server seed: a bare 4-byte Big-Endian key.

    The phase-1 (account) connection opens with the 21-byte 0xEF Seed packet
    (``build_seed``). The phase-2 (game) connection, however, is prefixed with
    only the raw 4-byte auth key from the 0x8C relay — there is NO 0xEF header
    and no version fields. See ClassicUO ``LoginScene.HandleRelayServerPacket``,
    which writes ``{seed>>24, seed>>16, seed>>8, seed}`` before ``Send_SecondLogin``.
    """
    w = PacketWriter()
    w.write_u32(seed)
    return w.to_bytes()


def build_account_login(username: str, password: str) -> bytes:
    """Build AccountLogin packet (0x80, 62 bytes)."""
    w = PacketWriter()
    w.write_u8(0x80)
    w.write_ascii(username, 30)
    w.write_ascii(password, 30)
    w.write_u8(0xFF)  # next_login_key
    return w.to_bytes()


def build_server_select(index: int) -> bytes:
    """Build ServerSelect packet (0xA0, 3 bytes)."""
    w = PacketWriter()
    w.write_u8(0xA0)
    w.write_u16(index)
    return w.to_bytes()


def build_game_login(auth_key: int, username: str, password: str) -> bytes:
    """Build GameLogin packet (0x91, 65 bytes)."""
    w = PacketWriter()
    w.write_u8(0x91)
    w.write_u32(auth_key)
    w.write_ascii(username, 30)
    w.write_ascii(password, 30)
    return w.to_bytes()


def build_delete_character(password: str, slot: int, client_ip: int = 0x7F000001) -> bytes:
    """Build DeleteCharacter packet (0x83, 39 bytes).

    Layout per ClassicUO ``Send_DeleteCharacter``
    (src/ClassicUO.Client/Network/OutgoingPackets.cs):

        [0x83][30 zero bytes][slot:u32 BE][clientIP:u32 BE]

    The 30-byte field is *all zeros* — it is NOT the account password. Modern
    clients stopped putting the password on the wire here, and ServUO's
    ``PacketHandlers.DeleteCharacter`` simply ``Seek(30, ...)`` past it before
    reading the slot. The previous build wrote the cleartext password into this
    field, which (a) diverges from the reference wire bytes and (b) needlessly
    leaks the password to any server/proxy that *does* read those 30 bytes. The
    ``password`` parameter is retained for call-site compatibility but ignored.
    """
    del password  # field is reserved/zeroed on the wire (matches ClassicUO)
    w = PacketWriter()
    w.write_u8(0x83)
    w.write_zeros(30)
    w.write_u32(slot)
    w.write_u32(client_ip)
    return w.to_bytes()


# Facet-access bits ServUO stores in ``state.Flags`` from the 0x5D client-flags
# field (Server/ExpansionInfo.cs ``ClientFlags``): Felucca 0x01 | Trammel 0x02 |
# Ilshenar 0x04 | Malas 0x08 | Tokuno 0x10 | TerMur 0x20 = 0x3F. We advertise a
# modern 7.0.102.3 client (``build_seed``), which ClassicUO mirrors by writing
# its negotiated ``Protocol`` flags into this field. A full-expansion client
# enables every facet, so 0x3F is the matching value.
_ALL_FACET_CLIENT_FLAGS = 0x3F


def build_play_character(
    name: str = "",
    slot: int = 0,
    client_ip: int = 0x7F000001,
    client_flags: int = _ALL_FACET_CLIENT_FLAGS,
) -> bytes:
    """Build PlayCharacter packet (0x5D, 73 bytes).

    ServUO ``PacketHandlers.PlayCharacter`` reads the 4-byte client-flags field
    (offset 36) and assigns ``state.Flags = (ClientFlags)flags``. ClassicUO
    fills it with its negotiated ``Protocol`` value (``Send_SelectCharacter``);
    sending a bare ``0`` makes the server record ``ClientFlags.None``, so
    facet-/expansion-aware server logic (e.g. ``NetState.IsUOTDClient`` and
    facet access) sees a client that claims support for nothing. Default to the
    full-facet mask (0x3F) that matches the modern client version we advertise
    so ``state.Flags`` reflects reality instead of an empty expansion set.
    """
    w = PacketWriter()
    w.write_u8(0x5D)
    w.write_u32(0xEDEDEDED)  # pattern
    w.write_ascii(name, 30)
    w.write_zeros(2)  # unknown
    w.write_u32(client_flags)  # client/facet flags (ServUO -> state.Flags)
    w.write_zeros(24)  # unknown
    w.write_u32(slot)
    w.write_u32(client_ip)
    return w.to_bytes()


def build_walk_request(direction: int, seq: int, fastwalk: int = 0) -> bytes:
    """Build WalkRequest packet (0x02, 7 bytes)."""
    w = PacketWriter()
    w.write_u8(0x02)
    w.write_u8(direction & 0xFF)
    w.write_u8(seq & 0xFF)
    w.write_u32(fastwalk)
    return w.to_bytes()


def build_ping(seq: int) -> bytes:
    """Build Ping packet (0x73, 2 bytes)."""
    w = PacketWriter()
    w.write_u8(0x73)
    w.write_u8(seq & 0xFF)
    return w.to_bytes()


def build_attack(serial: int) -> bytes:
    """Build Attack packet (0x05, 5 bytes)."""
    w = PacketWriter()
    w.write_u8(0x05)
    w.write_u32(serial)
    return w.to_bytes()


def build_double_click(serial: int) -> bytes:
    """Build DoubleClick packet (0x06, 5 bytes)."""
    w = PacketWriter()
    w.write_u8(0x06)
    w.write_u32(serial)
    return w.to_bytes()


def build_single_click(serial: int) -> bytes:
    """Build SingleClick packet (0x09, 5 bytes)."""
    w = PacketWriter()
    w.write_u8(0x09)
    w.write_u32(serial)
    return w.to_bytes()


def _encode_keywords(keyword_ids: list[int]) -> bytes:
    """Pack keyword IDs as 12-bit values (ClassicUO format).

    Format: [count_hi byte] then alternating pairs of IDs packed
    into 3 bytes per pair using a carried-nibble scheme.
    """
    count = len(keyword_ids)
    result = bytearray()
    result.append((count >> 4) & 0xFF)
    carry = count & 0x0F
    flag = False

    for kw_id in keyword_ids:
        if flag:
            result.append((kw_id >> 4) & 0xFF)
            carry = kw_id & 0x0F
        else:
            result.append(((carry << 4) | ((kw_id >> 8) & 0x0F)) & 0xFF)
            result.append(kw_id & 0xFF)
        flag = not flag

    if not flag:
        result.append((carry << 4) & 0xFF)

    return bytes(result)


# Common speech keywords (from speech.mul) — used by ServUO NPC dispatch.
# Banker keywords (0x0000–0x0003) map to the switch in
# Scripts/Mobiles/NPCs/Banker.cs. Without the keyword encoding the
# server receives only text, and Banker.HandleSpeech never fires.
SPEECH_KEYWORDS: dict[str, list[int]] = {
    "withdraw": [0x0000],
    "balance": [0x0001],
    "bank": [0x0002],
    "check": [0x0003],
    "vendor sell": [0x014D],
    "vendor buy": [0x003C],
    "guards": [0x0007],
}


def _match_keywords(text: str) -> list[int]:
    """Match text against known speech keywords."""
    text_lower = text.lower()
    matched: list[int] = []
    for phrase, ids in SPEECH_KEYWORDS.items():
        if phrase in text_lower:
            matched.extend(ids)
    return sorted(set(matched))


def build_unicode_speech(
    text: str,
    msg_type: int = 0,
    hue: int = 0x0034,
    font: int = 3,
    lang: str = "ENU",
) -> bytes:
    """Build UnicodeSpeech packet (0xAD, variable).

    Automatically detects keywords (bank, vendor sell, etc.) and
    encodes them using the 12-bit keyword format that ServUO expects.
    Without keyword encoding, NPC speech handlers won't trigger.

    ServUO's ``UnicodeSpeech`` handler trims the text and then *silently
    drops the whole packet* when ``text.Length > 128`` (PacketHandlers.cs:
    ``if (text.Length <= 0 || text.Length > 128) return;``). The LLM reply
    path caps responses at 200 chars, so any 129-200 char reply was framed,
    sent, and discarded server-side — the agent looked like it answered but
    said nothing. Clamp the body to 128 UTF-16 code units (the server's own
    measure) up front, mirroring the 239-cap gump-text precedent below. The
    cap is applied before keyword matching so a clamped reply can still carry
    a banker/vendor keyword that survived the truncation.
    """
    # Server measures length in UTF-16 code units (C# String.Length); clamp on
    # the same unit so astral chars (2 units) can't push the trimmed text over.
    if len(text.encode("utf-16-be")) // 2 > 128:
        units = text.encode("utf-16-be")[: 128 * 2]
        text = units.decode("utf-16-be", errors="ignore")
    keywords = _match_keywords(text)

    w = PacketWriter()
    w.write_u8(0xAD)
    w.write_u16(0)  # placeholder for length

    if keywords:
        # Keyword-encoded mode: type |= 0xC0, text as UTF-8
        w.write_u8(msg_type | 0xC0)
        w.write_u16(hue)
        w.write_u16(font)
        w.write_ascii(lang, 4)
        w.write_bytes(_encode_keywords(keywords))
        w.write_bytes(text.encode("utf-8") + b"\x00")
    else:
        # Plain mode: text as UTF-16 BE
        w.write_u8(msg_type)
        w.write_u16(hue)
        w.write_u16(font)
        w.write_ascii(lang, 4)
        w.write_bytes(text.encode("utf-16-be") + b"\x00\x00")

    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_ascii_speech(
    text: str,
    msg_type: int = 0,
    hue: int = 0x0034,
    font: int = 3,
) -> bytes:
    """Build the legacy AsciiSpeech packet (0x03, variable).

    Mirrors ClassicUO ``Send_ASCIISpeechRequest`` (the pre-CV_200 path of
    ``GameActions.Say``) exactly:

        [ID 0x03][len u16 BE][type u8][hue u16 BE][font u16 BE][ascii text + 0x00]

    Field widths are the easy thing to get wrong: ``hue`` and ``font`` are
    *both* 16-bit big-endian (font is a ``byte`` argument in ClassicUO but
    serialised with ``WriteUInt16BE``), and the text is null-terminated
    Cp1252/ASCII — not UTF-16 like the 0xAD unicode variant.

    ``msg_type`` carries the speech mode (0 = say/Regular, 2 = emote,
    8 = whisper, 9 = yell). When a known keyword phrase is present the high
    ``Encoded`` bit (0xC0) is OR-ed into the type so ServUO's keyword dispatch
    fires. Unlike 0xAD, the 0x03 frame does *not* carry packed keyword bytes —
    only the type flag changes; the body stays plain ASCII.
    """
    encoded = bool(_match_keywords(text))

    w = PacketWriter()
    w.write_u8(0x03)
    w.write_u16(0)  # placeholder for length
    w.write_u8((msg_type | 0xC0) if encoded else (msg_type & 0xFF))
    w.write_u16(hue)
    w.write_u16(font)
    w.write_bytes(text.encode("ascii", errors="replace") + b"\x00")

    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_war_mode(war: bool) -> bytes:
    """Build WarMode packet (0x72, 5 bytes)."""
    w = PacketWriter()
    w.write_u8(0x72)
    w.write_u8(1 if war else 0)
    # Wire layout per ClassicUO Send_ChangeWarMode: [ID][state][0x32][0x00],
    # then a single zero pad byte to reach the fixed length of 5 (0x72 is a
    # fixed-5 packet in PacketsTable). The 0x32 byte must come *immediately*
    # after the war flag — the previous order (0x00, 0x32) shipped 0x32 one
    # byte too late, so every war-mode toggle sent a malformed frame.
    w.write_u8(0x32)  # magic (ClassicUO writes this right after the flag)
    w.write_u8(0x00)  # magic
    w.write_u8(0x00)  # pad to fixed length 5
    return w.to_bytes()


def build_status_request(request_type: int, serial: int) -> bytes:
    """Build StatusRequest packet (0x34, 10 bytes)."""
    w = PacketWriter()
    w.write_u8(0x34)
    w.write_u32(0xEDEDEDED)  # pattern
    w.write_u8(request_type)  # 4 = basic stats, 5 = skills
    w.write_u32(serial)
    return w.to_bytes()


def build_client_version(version: str) -> bytes:
    """Build ClientVersion packet (0xBD, variable)."""
    w = PacketWriter()
    w.write_u8(0xBD)
    w.write_u16(0)  # placeholder for length
    encoded = version.encode("ascii") + b"\x00"
    w.write_bytes(encoded)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_opl_request(serial: int) -> bytes:
    """Build MegaCliloc batch request (0xD6, variable).

    Requests OPL (Object Property List) for one or more serials.
    """
    w = PacketWriter()
    w.write_u8(0xD6)
    w.write_u16(0)  # length placeholder
    w.write_u32(serial)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_target_response(
    target_type: int,  # 0=object, 1=location
    cursor_id: int,  # cursor ID from server's target request
    serial: int = 0,  # target entity serial (0 for ground)
    x: int = 0,
    y: int = 0,
    z: int = 0,
    graphic: int = 0,  # tile graphic (for ground targets)
    cursor_flag: int = 0,  # echo of server cursor flag: 0=neutral,1=harmful,2=helpful
) -> bytes:
    """Build TargetResponse packet (0x6C, 19 bytes).

    Layout (matches ClassicUO Send_TargetObject/Send_TargetXYZ):
    ``[0x6C][type:u8][cursorID:u32][cursorFlag:u8][serial:u32][x:u16][y:u16][z:u16][graphic:u16]``.
    The cursor flag byte must echo the flag from the server's 0x6C request;
    several servers reject a response whose flag does not match the request.
    """
    w = PacketWriter()
    w.write_u8(0x6C)
    w.write_u8(target_type)
    w.write_u32(cursor_id)
    w.write_u8(cursor_flag & 0xFF)  # echo server's cursor flag (was hardcoded 0)
    w.write_u32(serial)
    w.write_u16(x)
    w.write_u16(y)
    w.write_u16(z & 0xFFFF)  # signed i16 as unsigned
    w.write_u16(graphic)
    return w.to_bytes()


def build_use_skill(skill_id: int) -> bytes:
    """Build UseSkill packet (0x12 TextCommand, sub-type 0x24, variable).

    Wire payload is the ASCII command ``"{skill_id} 0\\0"`` (ClassicUO
    ``Send_UseSkill``: ``WriteUInt8(0x24)`` then ``WriteASCII($"{idx} 0")``,
    where ``WriteASCII`` appends the NUL terminator). The trailing ``0`` is
    ignored by ServUO, which parses ``command.Split(' ')[0]`` as the index.

    ClassicUO ``GameActions.UseSkill`` refuses to send for ``index < 0``,
    and ServUO ``Skills.UseSkill`` discards any ``skillID`` outside
    ``[0, SkillInfo.Table.Length)``. A negative id therefore produces a
    packet the server silently drops while the action layer still reports
    success — a false "skill used". Reject it at the source instead so the
    bug surfaces here rather than as an invisible no-op on the wire.
    """
    if skill_id < 0:
        raise ValueError(f"skill_id must be >= 0, got {skill_id}")
    w = PacketWriter()
    w.write_u8(0x12)
    w.write_u16(0)  # length placeholder
    w.write_u8(0x24)  # type: skill
    command = f"{skill_id} 0\x00".encode("ascii")
    w.write_bytes(command)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_cast_spell(spell_id: int) -> bytes:
    """Build CastSpell packet (0x12, variable)."""
    w = PacketWriter()
    w.write_u8(0x12)
    w.write_u16(0)  # length placeholder
    w.write_u8(0x56)  # type: spell
    command = f"{spell_id}\x00".encode("ascii")
    w.write_bytes(command)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_pick_up(serial: int, amount: int = 1) -> bytes:
    """Build PickUp packet (0x07, 7 bytes).

    ``amount`` is the stack count, a u16 on the wire (ClassicUO
    ``Send_PickUpRequest``). Clamp it to [0, 0xFFFF] so a computed
    quantity that overflows preserves intent ("lift as much as
    possible") instead of silently wrapping mod 65536 into a smaller,
    valid-looking — but wrong — lift the server would honour.
    """
    w = PacketWriter()
    w.write_u8(0x07)
    w.write_u32(serial)
    w.write_u16(max(0, min(amount, 0xFFFF)))
    return w.to_bytes()


def build_drop_item(
    serial: int,
    x: int = 0xFFFF,
    y: int = 0xFFFF,
    z: int = 0,
    container: int = 0xFFFFFFFF,
) -> bytes:
    """Build DropItem packet (0x08, 15 bytes).

    ``z`` is the drop altitude, an sbyte on the wire (ClassicUO
    ``Send_DropRequest`` ``WriteInt8``). World/spawn-spot z values flow
    in straight from ``WorldState`` as plain Python ints; a value outside
    the signed-byte range would make ``write_i8``'s ``struct.pack('b', ...)``
    raise ``struct.error`` and abort the whole drop. Clamp to [-128, 127]
    so an extreme altitude lands the item at the nearest valid height
    instead of crashing the pick-up -> drop sequence mid-flight.
    """
    w = PacketWriter()
    w.write_u8(0x08)
    w.write_u32(serial)
    w.write_u16(x)
    w.write_u16(y)
    w.write_i8(max(-128, min(z, 127)))
    w.write_u8(0x00)  # grid index
    w.write_u32(container)
    return w.to_bytes()


def build_equip_item(serial: int, layer: int, mobile_serial: int) -> bytes:
    """Build EquipItem packet (0x13, 10 bytes)."""
    w = PacketWriter()
    w.write_u8(0x13)
    w.write_u32(serial)
    w.write_u8(layer)
    w.write_u32(mobile_serial)
    return w.to_bytes()


# ServUO reads the per-item vendor amount as a *signed* Int16 in both
# VendorBuyReply (`int amount = pvSrc.ReadInt16();` then drops when
# `amount <= 0`) and VendorSellReply (`int Amount = pvSrc.ReadInt16();`
# then keeps only `Amount > 0`). PacketWriter.write_u16 masks with
# `& 0xFFFF` and never raises, so an amount >= 32768 (e.g. a stack of
# 40000 logs/ingots/ore) wraps to a negative server-side value and the
# whole item is silently dropped from the transaction — the procedure
# then reports "expected ~Ngp but got 0". Clamp to [1, 32767] so the
# planned amount always survives the round-trip. Mirrors the signed-byte
# z-clamp already done in build_drop_item.
_VENDOR_AMOUNT_MAX = 0x7FFF  # 32767 — largest positive signed Int16


def _clamp_vendor_amount(amount: int) -> int:
    """Clamp a vendor buy/sell amount into ServUO's signed-positive range."""
    if amount < 1:
        return 1
    if amount > _VENDOR_AMOUNT_MAX:
        return _VENDOR_AMOUNT_MAX
    return amount


def build_buy_items(vendor_serial: int, items: list[tuple[int, int]]) -> bytes:
    """Build BuyItems packet (0x3B, variable)."""
    w = PacketWriter()
    w.write_u8(0x3B)
    w.write_u16(0)  # length placeholder
    w.write_u32(vendor_serial)
    w.write_u8(0x02 if items else 0x00)  # flag: 0x02 = items follow
    for item_serial, amount in items:
        w.write_u8(0x1A)  # item layer flag
        w.write_u32(item_serial)
        w.write_u16(_clamp_vendor_amount(amount))
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_gump_response(
    serial: int,
    gump_id: int,
    button_id: int,
    switches: list[int] | None = None,
    text_entries: list[tuple[int, str]] | None = None,
) -> bytes:
    """Build GumpResponse packet (0xB1, variable).

    Args:
        serial: Player character serial.
        gump_id: Gump type ID (from the OpenGump packet).
        button_id: Button pressed (0 = close/cancel).
        switches: List of active switch/checkbox IDs.
        text_entries: List of (entry_id, text) for text input fields.
    """
    switches = switches or []
    text_entries = text_entries or []

    w = PacketWriter()
    w.write_u8(0xB1)
    w.write_u16(0)  # length placeholder
    w.write_u32(serial)
    w.write_u32(gump_id)
    w.write_u32(button_id)
    # Switches
    w.write_u32(len(switches))
    for sw in switches:
        w.write_u32(sw)
    # Text entries
    w.write_u32(len(text_entries))
    for entry_id, text in text_entries:
        w.write_u16(entry_id)
        # The server reads `char_len` UTF-16 code units; if it exceeds 239 it
        # treats the response as malformed and *disconnects* the client
        # (ServUO PacketHandlers: `if (textLength > 239) { ...disconnecting... }`).
        # ClassicUO clamps identically via `Math.Min(239, text.Length)`, so we
        # truncate on encoded code-unit count and keep the declared count in
        # sync with the bytes actually written (correct even for astral chars,
        # which occupy two UTF-16 units).
        encoded = text.encode("utf-16-be")
        char_len = len(encoded) // 2
        if char_len > 239:
            char_len = 239
            encoded = encoded[: char_len * 2]
        w.write_u16(char_len)
        w.write_bytes(encoded)

    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_sell_items(vendor_serial: int, items: list[tuple[int, int]]) -> bytes:
    """Build SellItems packet (0x9F, variable)."""
    w = PacketWriter()
    w.write_u8(0x9F)
    w.write_u16(0)  # length placeholder
    w.write_u32(vendor_serial)
    w.write_u16(len(items))
    for item_serial, amount in items:
        w.write_u32(item_serial)
        w.write_u16(_clamp_vendor_amount(amount))
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_skill_lock(skill_id: int, lock_state: int) -> bytes:
    """Build SkillLock packet (0x3A, variable).

    lock_state: 0=Up, 1=Down, 2=Locked
    """
    w = PacketWriter()
    w.write_u8(0x3A)
    w.write_u16(0)  # length placeholder
    w.write_u16(skill_id)
    w.write_u8(lock_state)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_context_menu_request(serial: int) -> bytes:
    """Build ContextMenuRequest packet (0xBF subcommand 0x13, variable)."""
    w = PacketWriter()
    w.write_u8(0xBF)
    w.write_u16(0)  # length placeholder
    w.write_u16(0x0013)
    w.write_u32(serial)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_context_menu_selection(serial: int, index: int) -> bytes:
    """Build ContextMenuResponse packet (0xBF subcommand 0x15, variable)."""
    w = PacketWriter()
    w.write_u8(0xBF)
    w.write_u16(0)  # length placeholder
    w.write_u16(0x0015)
    w.write_u32(serial)
    w.write_u16(index)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_stat_lock(stat_index: int, lock_state: int) -> bytes:
    """Build StatLock packet (0xBF subcommand 0x1A, variable).

    stat_index: 0=STR, 1=DEX, 2=INT
    lock_state: 0=Up, 1=Down, 2=Locked
    """
    w = PacketWriter()
    w.write_u8(0xBF)
    w.write_u16(0)  # length placeholder
    w.write_u16(0x001A)  # subcommand: SetStatLock
    w.write_u8(stat_index)
    w.write_u8(lock_state)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_trade_cancel(serial: int) -> bytes:
    """Build a SecureTrade cancel packet (0x6F, variable).

    Verified against ClassicUO ``Send_TradeResponse`` with ``code == 1``
    (src/ClassicUO.Client/Network/OutgoingPackets.cs):

        ID 0x6F | len(u16 BE) | action 0x01 | trade-serial(u32 BE)

    ``serial`` is the trade-window serial that the server assigned in the
    incoming 0x6F type-0 ("open") packet — NOT a mobile serial. Closing the
    trade window is what the server treats as a cancel.
    """
    w = PacketWriter()
    w.write_u8(0x6F)
    w.write_u16(0)  # length placeholder
    w.write_u8(0x01)  # action: cancel
    w.write_u32(serial & 0xFFFFFFFF)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_trade_accept(serial: int, accept: bool) -> bytes:
    """Build a SecureTrade set-accept packet (0x6F, variable).

    Verified against ClassicUO ``Send_TradeResponse`` with ``code == 2``:

        ID 0x6F | len(u16 BE) | action 0x02 | trade-serial(u32 BE) | state(u32 BE)

    The accept flag is written as a *u32* (0 or 1), not a single byte — the
    bug this guards against is sending a 1-byte bool, which desyncs the
    server's 4-byte read and leaves the trade stuck "not accepted".
    """
    w = PacketWriter()
    w.write_u8(0x6F)
    w.write_u16(0)  # length placeholder
    w.write_u8(0x02)  # action: set-accept
    w.write_u32(serial & 0xFFFFFFFF)
    w.write_u32(1 if accept else 0)
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_trade_update_gold(serial: int, gold: int, platinum: int = 0) -> bytes:
    """Build a SecureTrade update-gold packet (0x6F, variable).

    Verified against ClassicUO ``Send_TradeUpdateGold``:

        ID 0x6F | len(u16 BE) | action 0x03
               | trade-serial(u32 BE) | gold(u32 BE) | platinum(u32 BE)

    Gold and platinum amounts are clamped to the u32 range so a computed
    amount that overflows preserves intent ("offer as much as we have")
    instead of silently wrapping mod 2**32 into a smaller, valid-looking
    offer the server would honour.
    """
    w = PacketWriter()
    w.write_u8(0x6F)
    w.write_u16(0)  # length placeholder
    w.write_u8(0x03)  # action: update gold
    w.write_u32(serial & 0xFFFFFFFF)
    w.write_u32(max(0, min(gold, 0xFFFFFFFF)))
    w.write_u32(max(0, min(platinum, 0xFFFFFFFF)))
    data = bytearray(w.to_bytes())
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)
