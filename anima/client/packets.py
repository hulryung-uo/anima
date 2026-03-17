"""UO packet definitions: length table, outgoing builders, and incoming parsers."""

from __future__ import annotations

from anima.client.codec import PacketWriter

# ---------------------------------------------------------------------------
# Packet length table (from servuo-rs PACKET_LENGTHS)
#   > 0 = fixed length (including the 1-byte packet ID)
#   0   = variable length (bytes 1-2 = u16 BE total length)
#   -1  = unknown / unhandled
# ---------------------------------------------------------------------------

PACKET_LENGTHS: dict[int, int] = {
    # Complete packet length table based on ClassicUO PacketsTable.cs
    # >0 = fixed length (including ID byte), 0 = variable (bytes 1-2 = BE u16 length)
    0x00: 104, 0x01: 5,   0x02: 7,   0x03: 0,   0x04: 2,   0x05: 5,   0x06: 5,   0x07: 7,
    0x08: 15,  0x09: 5,   0x0A: 11,  0x0B: 7,   0x0C: 0,   0x0D: 3,   0x0E: 0,   0x0F: 61,
    0x10: 0,   0x11: 0,   0x12: 0,   0x13: 10,  0x14: 6,   0x15: 9,   0x16: 1,   0x17: 0,
    0x18: 0,   0x19: 0,   0x1A: 0,   0x1B: 37,  0x1C: 0,   0x1D: 5,   0x1E: 4,   0x1F: 8,
    0x20: 19,  0x21: 8,   0x22: 3,   0x23: 26,  0x24: 9,   0x25: 21,  0x26: 0,   0x27: 2,
    0x28: 5,   0x29: 1,   0x2A: 5,   0x2B: 2,   0x2C: 2,   0x2D: 17,  0x2E: 15,  0x2F: 10,
    0x30: 5,   0x31: 1,   0x32: 2,   0x33: 0,   0x34: 10,  0x35: 0,   0x36: 0,   0x37: 8,
    0x38: 7,   0x39: 0,   0x3A: 0,   0x3B: 0,   0x3C: 0,   0x3E: 37,  0x3F: 0,
    0x40: 0,   0x41: 0,   0x42: 0,   0x43: 0,   0x44: 0,   0x45: 5,   0x46: 0,   0x47: 11,
    0x48: 73,  0x49: 63,  0x4E: 6,   0x4F: 2,
    0x54: 12,  0x55: 1,   0x56: 11,  0x57: 110, 0x58: 106, 0x5B: 4,   0x5D: 73,
    0x65: 4,   0x66: 0,   0x6C: 19,  0x6D: 3,   0x6E: 14,  0x6F: 0,   0x70: 28,
    0x71: 0,   0x72: 5,   0x73: 2,   0x74: 0,   0x75: 35,  0x76: 16,  0x77: 17,  0x78: 0,
    0x7C: 0,   0x7D: 13,
    0x80: 62,  0x82: 2,   0x83: 39,  0x85: 2,   0x86: 0,   0x88: 66,  0x89: 0,
    0x8C: 11,  0x90: 19,  0x91: 65,  0x93: 99,  0x95: 9,   0x97: 2,   0x98: 0,
    0x99: 0,   0x9A: 0,   0x9B: 258, 0x9E: 0,   0x9F: 0,
    0xA0: 3,   0xA1: 9,   0xA2: 9,   0xA3: 9,   0xA4: 149, 0xA5: 0,   0xA6: 0,
    0xA7: 4,   0xA8: 0,   0xA9: 0,   0xAA: 5,   0xAB: 0,   0xAD: 0,   0xAE: 0,
    0xAF: 13,  0xB0: 0,   0xB1: 0,   0xB2: 0,   0xB5: 64,  0xB6: 9,   0xB7: 0,
    0xB8: 0,   0xB9: 5,   0xBA: 6,   0xBB: 9,   0xBC: 3,   0xBD: 0,   0xBE: 0,
    0xBF: 0,   0xC0: 36,  0xC1: 0,   0xC2: 0,   0xC4: 6,   0xC7: 49,  0xC8: 2,
    0xCA: 6,   0xCB: 7,   0xCC: 0,   0xCF: 0,   0xD0: 0,   0xD1: 2,   0xD2: 25,
    0xD3: 0,   0xD4: 0,   0xD6: 0,   0xD7: 0,   0xD8: 0,   0xD9: 0,   0xDB: 0,
    0xDC: 9,   0xDD: 0,   0xDE: 0,   0xDF: 0,
    0xE1: 0,   0xE2: 10,  0xE3: 0,   0xE5: 0,   0xE6: 5,
    0xEC: 0,   0xED: 0,   0xEF: 21,  0xF0: 0,   0xF1: 0,   0xF3: 26,  0xF4: 0,
    0xF5: 21,  0xF6: 0,   0xF7: 0,   0xF8: 106, 0xFB: 2,   0xFD: 2,
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


def build_play_character(name: str = "", slot: int = 0, client_ip: int = 0x7F000001) -> bytes:
    """Build PlayCharacter packet (0x5D, 73 bytes)."""
    w = PacketWriter()
    w.write_u8(0x5D)
    w.write_u32(0xEDEDEDED)  # pattern
    w.write_ascii(name, 30)
    w.write_zeros(2)  # unknown
    w.write_u32(0)  # client flags
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


def build_unicode_speech(
    text: str,
    msg_type: int = 0,
    hue: int = 0x0034,
    font: int = 3,
    lang: str = "ENU",
) -> bytes:
    """Build UnicodeSpeech packet (0xAD, variable)."""
    w = PacketWriter()
    w.write_u8(0xAD)
    w.write_u16(0)  # placeholder for length
    w.write_u8(msg_type)
    w.write_u16(hue)
    w.write_u16(font)
    w.write_ascii(lang, 4)
    # No keyword encoding — write raw unicode
    encoded = text.encode("utf-16-be") + b"\x00\x00"
    w.write_bytes(encoded)
    data = bytearray(w.to_bytes())
    # Fill in length
    length = len(data)
    data[1] = (length >> 8) & 0xFF
    data[2] = length & 0xFF
    return bytes(data)


def build_war_mode(war: bool) -> bytes:
    """Build WarMode packet (0x72, 5 bytes)."""
    w = PacketWriter()
    w.write_u8(0x72)
    w.write_u8(1 if war else 0)
    w.write_u8(0x00)  # unknown
    w.write_u8(0x32)  # unknown
    w.write_u8(0x00)  # unknown
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
