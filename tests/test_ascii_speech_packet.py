"""Wire-layout tests for the legacy AsciiSpeech builder (0x03).

Pinned against ClassicUO ``Send_ASCIISpeechRequest``:

    [ID 0x03][len u16 BE][type u8][hue u16 BE][font u16 BE][ascii + 0x00]

The field widths are the load-bearing detail: hue and font are *both*
16-bit big-endian, the text is null-terminated ASCII (not UTF-16), and the
self-described u16 length must equal the real frame length.
"""

from __future__ import annotations

import struct

from anima.client.packets import build_ascii_speech


def _split(pkt: bytes) -> tuple[int, int, int, int, int, bytes]:
    """Decode (id, length, type, hue, font, text-incl-terminator)."""
    pid = pkt[0]
    length = struct.unpack(">H", pkt[1:3])[0]
    msg_type = pkt[3]
    hue = struct.unpack(">H", pkt[4:6])[0]
    font = struct.unpack(">H", pkt[6:8])[0]
    return pid, length, msg_type, hue, font, pkt[8:]


class TestAsciiSpeechPacket:
    def test_packet_id_and_self_length(self) -> None:
        pkt = build_ascii_speech("hello")
        pid, length, *_ = _split(pkt)
        assert pid == 0x03
        # The embedded u16 length must equal the real frame length (incl. ID).
        assert length == len(pkt)

    def test_field_widths_hue_and_font_are_u16_be(self) -> None:
        # Distinct, asymmetric values catch a u8/u16 or LE/BE mix-up.
        pkt = build_ascii_speech("hi", hue=0x0102, font=0x0304)
        _, _, _, hue, font, _ = _split(pkt)
        assert hue == 0x0102
        assert font == 0x0304
        # Header is exactly 8 bytes before the text => hue/font each took 2.
        assert pkt[8:] == b"hi\x00"

    def test_text_is_null_terminated_ascii(self) -> None:
        pkt = build_ascii_speech("balance")
        text = pkt[8:]
        assert text.endswith(b"\x00")
        assert text == b"balance\x00"
        # ASCII, not UTF-16: 7 chars + terminator, no interleaved nulls.
        assert b"\x00" not in text[:-1]

    def test_default_say_mode_is_regular(self) -> None:
        pkt = build_ascii_speech("hello")  # no keyword -> plain
        _, _, msg_type, hue, font, _ = _split(pkt)
        assert msg_type == 0x00  # Regular / say
        assert hue == 0x0034
        assert font == 3

    def test_speech_modes_pass_through(self) -> None:
        # Regular=0, Emote=2, Whisper=8, Yell=9 (ClassicUO MessageType).
        for mode in (0x00, 0x02, 0x08, 0x09):
            pkt = build_ascii_speech("die", msg_type=mode)
            _, _, msg_type, *_ = _split(pkt)
            assert msg_type == mode

    def test_keyword_sets_encoded_bit_without_keyword_bytes(self) -> None:
        # "balance" is a known banker keyword -> Encoded (0xC0) OR-ed into type,
        # but the 0x03 frame carries NO packed keyword bytes (unlike 0xAD):
        # the body must still be plain null-terminated ASCII.
        pkt = build_ascii_speech("balance")
        _, length, msg_type, _, _, text = _split(pkt)
        assert msg_type & 0xC0 == 0xC0
        assert text == b"balance\x00"
        assert length == len(pkt)

    def test_keyword_preserves_low_mode_bits(self) -> None:
        # Yelling a keyword: low nibble (mode 9) survives the Encoded OR.
        pkt = build_ascii_speech("guards", msg_type=0x09)
        _, _, msg_type, *_ = _split(pkt)
        assert msg_type == (0x09 | 0xC0)

    def test_plain_text_has_no_encoded_bit(self) -> None:
        pkt = build_ascii_speech("just chatting")
        _, _, msg_type, *_ = _split(pkt)
        assert msg_type & 0xC0 == 0x00

    def test_full_byte_layout_pinned(self) -> None:
        # Exact golden frame for a keyword-encoded "balance".
        pkt = build_ascii_speech("balance", msg_type=0, hue=0x0034, font=3)
        expected = (
            b"\x03"          # ID
            b"\x00\x10"      # length = 16 bytes total
            b"\xc0"          # type: Regular | Encoded
            b"\x00\x34"      # hue u16 BE
            b"\x00\x03"      # font u16 BE
            b"balance\x00"   # null-terminated ASCII
        )
        assert pkt == expected
