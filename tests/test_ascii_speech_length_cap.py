"""build_ascii_speech (0x03) must trim + clamp to ServUO's 128-char gate.

ServUO's ``AsciiSpeech`` handler (Server/Network/PacketHandlers.cs) runs::

    string text = pvSrc.ReadStringSafe().Trim();
    if (text.Length <= 0 || text.Length > 128) { return; }

i.e. it trims surrounding whitespace *first*, then silently drops the entire
packet when the trimmed length is 0 or exceeds 128. This mirrors the 0xAD
UnicodeSpeech gate already guarded by ``build_unicode_speech``; the 0x03 builder
previously sent the raw, un-trimmed, un-clamped body, so a 129+ char reply (the
LLM say-path caps at 200) was framed, sent, and discarded server-side — the
agent "spoke" but said nothing. These tests pin the builder to the server's
trim-then-clamp ordering.
"""

from __future__ import annotations

import struct

from anima.client.packets import build_ascii_speech

SERVUO_MAX = 128


def _server_visible_text(pkt: bytes) -> str:
    """Replay ServUO's AsciiSpeech decode + Trim() for a 0x03 frame.

    Layout: [0x03][len u16 BE][type u8][hue u16 BE][font u16 BE][ascii + 0x00].
    The header before the text is 1+2+1+2+2 = 8 bytes, then a NUL-terminated
    single-byte string.
    """
    assert pkt[0] == 0x03
    declared = struct.unpack(">H", pkt[1:3])[0]
    assert declared == len(pkt)
    body = pkt[8:]
    assert body.endswith(b"\x00")
    # ServUO ReadStringSafe reads up to the NUL; .Trim() strips the edges.
    return body[:-1].decode("ascii", errors="replace").strip()


def test_overlong_reply_is_clamped_so_server_keeps_it() -> None:
    # 200 chars (the LLM say cap) must survive as <=128 the server accepts,
    # not be framed and silently dropped by the `Length > 128` gate.
    visible = _server_visible_text(build_ascii_speech("z" * 200))
    assert len(visible) == SERVUO_MAX
    assert 0 < len(visible) <= SERVUO_MAX


def test_surrounding_whitespace_does_not_steal_from_the_128_budget() -> None:
    # 128 real chars wrapped in whitespace: trimming first lands all 128;
    # a raw clamp would have spent units on the spaces and chopped content.
    reply = "  " + ("a" * 128) + "  "
    visible = _server_visible_text(build_ascii_speech(reply))
    assert visible == "a" * 128
    assert len(visible) == SERVUO_MAX


def test_whitespace_only_reply_collapses_to_empty_body() -> None:
    # The server Trim()s this to "" and drops the packet; the builder should
    # produce an empty (server-rejected) body, not a space-padded frame that
    # masquerades as real speech.
    pkt = build_ascii_speech("   \t  ")
    assert _server_visible_text(pkt) == ""


def test_short_unpadded_reply_is_unchanged() -> None:
    assert _server_visible_text(build_ascii_speech("Hello there!")) == "Hello there!"


def test_self_described_length_stays_consistent_after_clamp() -> None:
    pkt = build_ascii_speech("q" * 300)
    declared = struct.unpack(">H", pkt[1:3])[0]
    assert declared == len(pkt)
    # 8-byte header + 128 ascii chars + 1 NUL terminator.
    assert len(pkt) == 8 + SERVUO_MAX + 1


def test_padded_keyword_still_trips_encoded_bit_after_trim() -> None:
    # A padded banker keyword must still set the Encoded (0xC0) type bit once
    # the body is trimmed to the bare keyword.
    pkt = build_ascii_speech("  balance  ")
    msg_type = pkt[3]
    assert msg_type & 0xC0 == 0xC0
    assert pkt[8:] == b"balance\x00"


def test_keyword_match_runs_on_clamped_text() -> None:
    # A keyword that only appears past the 128-char boundary must NOT trip the
    # Encoded bit, since the server never sees it after the clamp.
    text = ("a" * 128) + " bank"
    pkt = build_ascii_speech(text)
    assert pkt[3] & 0xC0 == 0x00
