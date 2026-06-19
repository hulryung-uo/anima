"""Length-cap tests for the UnicodeSpeech builder (0xAD).

ServUO's ``UnicodeSpeech`` handler trims the incoming text and then bails out
entirely when it exceeds 128 characters::

    text = text.Trim();
    if (text.Length <= 0 || text.Length > 128) { return; }

i.e. the *whole packet is silently discarded* — the speech is never spoken.
The LLM reply path caps responses at 200 chars, comfortably above that limit,
so any 129-200 char reply used to be framed and sent yet dropped server-side.
``build_unicode_speech`` must therefore clamp the body to <= 128 UTF-16 code
units (the server's own measure) so the reply actually lands.
"""

from __future__ import annotations

import struct

from anima.client.packets import build_unicode_speech

SERVUO_MAX = 128


def _plain_text_units(pkt: bytes) -> int:
    """Decode the UTF-16 code-unit count of a plain (non-keyword) 0xAD frame.

    Layout: [0xAD][len u16][type u8][hue u16][font u16][lang 4][utf16be][00 00]
    The header before the text is 1+2+1+2+2+4 = 12 bytes; the body ends in a
    u16 NUL terminator.
    """
    assert pkt[0] == 0xAD
    declared = struct.unpack(">H", pkt[1:3])[0]
    assert declared == len(pkt)
    body = pkt[12:]
    assert body.endswith(b"\x00\x00")
    return (len(body) - 2) // 2


def test_overlong_plain_speech_is_clamped_to_128_units() -> None:
    # 200 chars (the LLM cap) — must come down to the server's 128 limit.
    pkt = build_unicode_speech("a" * 200)
    assert _plain_text_units(pkt) == SERVUO_MAX


def test_clamped_text_survives_server_length_check() -> None:
    # Replay the server's own gate: trim then require length in (0, 128].
    units = _plain_text_units(build_unicode_speech("z" * 500))
    assert 0 < units <= SERVUO_MAX


def test_short_speech_is_left_intact() -> None:
    pkt = build_unicode_speech("Hello there!")
    assert _plain_text_units(pkt) == len("Hello there!")


def test_exactly_128_is_not_truncated() -> None:
    pkt = build_unicode_speech("k" * 128)
    assert _plain_text_units(pkt) == 128


def test_clamp_never_splits_a_surrogate_pair() -> None:
    # An astral char is 2 UTF-16 units; a naive byte-slice could leave half a
    # surrogate pair and corrupt the frame. Build a string whose 128-unit
    # boundary lands mid-astral-char and confirm the body still decodes.
    body = ("a" * 127) + "\U0001F600" + ("b" * 10)  # emoji straddles unit 128
    pkt = build_unicode_speech(body)
    units = _plain_text_units(pkt)
    assert units <= SERVUO_MAX
    # The UTF-16BE payload (minus the u16 terminator) must round-trip cleanly.
    payload = pkt[12:-2]
    decoded = payload.decode("utf-16-be")  # raises on a dangling surrogate
    assert len(decoded) <= SERVUO_MAX


def test_keyword_packet_body_is_also_capped() -> None:
    # A keyword-bearing overlong line goes through the UTF-8 keyword path; the
    # decoded text must still be within the server limit.
    long_kw = "bank " + ("x" * 300)
    pkt = build_unicode_speech(long_kw)
    assert pkt[0] == 0xAD
    # type carries the Encoded (0xC0) bit when keywords matched.
    assert pkt[3] & 0xC0 == 0xC0
    # The trailing UTF-8 text (NUL-terminated) must be <= 128 chars.
    assert pkt.endswith(b"\x00")
    # Find the UTF-8 text: it is the tail after the 4-byte lang + keyword bytes.
    # Simplest robust check: the whole frame decoded text length stays bounded.
    # The first matched keyword "bank" survived the clamp, so encoding happened.
    text_region = pkt.split(b"ENU\x00", 1)[1]
    # Strip keyword bytes by locating the ascii 'bank' marker we know is present.
    idx = text_region.find(b"bank")
    assert idx != -1
    utf8_text = text_region[idx:-1].decode("utf-8", errors="ignore")
    assert len(utf8_text) <= SERVUO_MAX
