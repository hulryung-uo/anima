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
    0x00: 104,  # CreateCharacter
    0x01: 5,    # Disconnect
    0x02: 7,    # MovementRequest
    0x03: 0,    # AsciiSpeech (variable)
    0x04: 2,    # GodMode
    0x05: 5,    # Attack
    0x06: 5,    # DoubleClick
    0x07: 7,    # PickUpItem
    0x08: 15,   # DropItem
    0x09: 5,    # SingleClick
    0x0A: 11,   # Edit
    0x0B: 7,    # Damage
    0x11: 0,    # CharacterStatus (variable)
    0x12: 0,    # TextCommand (variable)
    0x13: 10,   # EquipItem
    0x14: 6,    # ChangeZ
    0x17: 0,    # HealthbarColor (variable)
    0x1A: 0,    # WorldItem (variable)
    0x1B: 37,   # LoginConfirm
    0x1C: 0,    # Talk (variable)
    0x1D: 5,    # DeleteObject
    0x1E: 0,    # MapPatch (variable)
    0x20: 19,   # MobileUpdate
    0x21: 8,    # DenyWalk
    0x22: 3,    # ConfirmWalk
    0x23: 26,   # DragAnimation
    0x24: 9,    # OpenContainer
    0x25: 21,   # ContainerItem
    0x26: 0,    # ContainerItemKR (variable)
    0x27: 2,    # PickUpRejected
    0x28: 5,    # DropAccepted
    0x29: 1,    # DropRejected
    0x2C: 2,    # DeathAnimation
    0x2E: 15,   # EquipmentUpdate
    0x2F: 10,   # Swing
    0x34: 10,   # StatusRequest
    0x36: 0,    # WarModeOld (variable)
    0x3A: 0,    # SkillUpdate (variable)
    0x3B: 0,    # BuyItems (variable)
    0x3C: 0,    # ContainerItems (variable)
    0x47: 11,   # PlayMidi
    0x48: 73,   # MapInfo
    0x4E: 6,    # PersonalLightLevel
    0x4F: 2,    # GlobalLightLevel
    0x54: 12,   # PlaySound
    0x55: 1,    # LoginComplete
    0x56: 11,   # MapEdit
    0x57: 110,  # UpdateRegion
    0x58: 106,  # NewRegion
    0x5B: 4,    # CurrentTime
    0x5D: 73,   # PlayCharacter
    0x65: 4,    # Weather
    0x66: 0,    # BookPages (variable)
    0x6C: 19,   # TargetResponse
    0x6D: 3,    # PlayMusic
    0x6E: 14,   # MobileAnimation
    0x6F: 0,    # SecureTrade (variable)
    0x71: 0,    # BulletinBoard (variable)
    0x72: 5,    # WarMode
    0x73: 2,    # Ping
    0x74: 0,    # VendorBuyList (variable)
    0x75: 35,   # RenameRequest
    0x77: 17,   # MobileMoving
    0x78: 0,    # MobileIncoming (variable)
    0x7D: 13,   # MenuResponse
    0x80: 62,   # AccountLogin
    0x82: 2,    # LoginDenied
    0x83: 39,   # CharacterDelete
    0x85: 2,    # CharacterDeleteResult
    0x86: 0,    # UpdateCharacterList (variable)
    0x88: 66,   # DisplayPaperdoll
    0x8C: 11,   # ServerRedirect
    0x90: 19,   # MapDisplay
    0x91: 65,   # GameLogin
    0x93: 99,   # BookHeaderOld
    0x95: 9,    # HueSelection
    0x98: 0,    # MobileName (variable)
    0x9A: 0,    # AsciiPromptReply (variable)
    0x9B: 258,  # HelpRequest
    0x9E: 0,    # SellList (variable)
    0x9F: 0,    # SellReply (variable)
    0xA0: 3,    # ServerSelect
    0xA1: 9,    # UpdateHitpoints
    0xA2: 9,    # UpdateMana
    0xA3: 9,    # UpdateStamina
    0xA4: 149,  # SystemInfo
    0xA7: 4,    # RequestScrollWindow
    0xA8: 0,    # ServerList (variable)
    0xA9: 0,    # CharacterList (variable)
    0xAA: 5,    # AttackCharacter
    0xAD: 0,    # UnicodeSpeech (variable)
    0xAE: 0,    # UnicodeTalk (variable)
    0xAF: 13,   # DisplayDeath
    0xB0: 0,    # DisplayGump (variable)
    0xB1: 0,    # GumpResponse (variable)
    0xB5: 64,   # ChatOpen
    0xB6: 9,    # ObjectHelp
    0xB8: 0,    # ProfileReq (variable)
    0xB9: 5,    # SupportedFeatures
    0xBB: 9,    # AccountID
    0xBC: 3,    # SeasonChange
    0xBD: 0,    # ClientVersion (variable)
    0xBE: 0,    # AssistVersion (variable)
    0xBF: 0,    # GeneralInfo (variable)
    0xC0: 36,   # GraphicalEffect
    0xC1: 0,    # MessageLocalized (variable)
    0xC2: 0,    # UnicodeSpeechPrompt (variable)
    0xC8: 2,    # ClientViewRange
    0xCC: 0,    # MessageLocalizedAffix (variable)
    0xCF: 0,    # AccountLogin2 (variable)
    0xD0: 0,    # ConfigurationFile (variable)
    0xD1: 2,    # Logout
    0xD4: 0,    # BookHeaderNew (variable)
    0xD6: 0,    # OPLData (variable)
    0xD7: 0,    # EncodedCommand (variable)
    0xD9: 0,    # HardwareInfo (variable)
    0xDF: 0,    # BuffDebuff (variable)
    0xE1: 0,    # ClientType (variable)
    0xE2: 10,   # NewCharacterAnimation
    0xEC: 0,    # EquipMacro (variable)
    0xED: 0,    # UnequipMacro (variable)
    0xEF: 21,   # Seed
    0xF0: 0,    # Krrios (variable)
    0xF1: 0,    # FreeshardListReq (variable)
    0xF3: 26,   # ObjectInfoSA
    0xF4: 0,    # CrashReport (variable)
    0xF5: 21,   # MapDiffRequest
    0xF8: 106,  # CreateCharacter70
    0xFB: 2,    # ShowPublicHouseContent
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
