"""build_unicode_speech (0xAD) must trim before the 128-unit clamp.

ServUO's ``UnicodeSpeech`` handler runs::

    text = text.Trim();
    if (text.Length <= 0 || text.Length > 128) { return; }

i.e. it trims surrounding whitespace *first*, then gates on length. The builder
previously clamped the raw, un-trimmed body to 128 UTF-16 code units, which (a)
spent part of the budget on leading/trailing whitespace and chopped real content
the server would otherwise have accepted, and (b) let a whitespace-only reply be
framed and sent only for the server to ``Trim()`` it to length 0 and drop the
whole packet. The builder must mirror the server's trim-then-clamp ordering.
"""

from __future__ import annotations

import struct

from anima.client.packets import build_unicode_speech

SERVUO_MAX = 128


def _server_visible_plain_text(pkt: bytes) -> str:
    """Replay ServUO's plain-path decode + Trim() for a 0xAD frame.

    Layout: [0xAD][len u16][type u8][hue u16][font u16][lang 4][utf16be][00 00].
    The header before the text is 1+2+1+2+2+4 = 12 bytes.
    """
    assert pkt[0] == 0xAD
    declared = struct.unpack(">H", pkt[1:3])[0]
    assert declared == len(pkt)
    body = pkt[12:]
    assert body.endswith(b"\x00\x00")
    return body[:-2].decode("utf-16-be").strip()


def test_surrounding_whitespace_does_not_steal_from_the_128_budget() -> None:
    # 128 real chars wrapped in whitespace. The old raw-clamp spent 2 units on
    # the spaces and chopped 2 content chars; after trimming first all 128 land.
    reply = "  " + ("a" * 128) + "  "
    visible = _server_visible_plain_text(build_unicode_speech(reply))
    assert visible == "a" * 128
    assert 0 < len(visible) <= SERVUO_MAX


def test_whitespace_only_reply_collapses_to_empty_body() -> None:
    # The server Trim()s this to "" and drops the packet; the builder should
    # produce an empty (server-rejected) body rather than a space-padded frame
    # that masquerades as real speech.
    pkt = build_unicode_speech("   \t  ")
    assert _server_visible_plain_text(pkt) == ""


def test_overlong_reply_still_clamps_after_trim() -> None:
    # Trimming must not defeat the existing 200->128 clamp.
    visible = _server_visible_plain_text(build_unicode_speech("  " + "z" * 300 + "  "))
    assert len(visible) == SERVUO_MAX


def test_short_unpadded_reply_is_unchanged() -> None:
    assert _server_visible_plain_text(build_unicode_speech("Hello there!")) == "Hello there!"


def test_trim_happens_before_keyword_match_survives() -> None:
    # A padded keyword line must still trip the Encoded (0xC0) path after trim.
    pkt = build_unicode_speech("  bank  ")
    assert pkt[0] == 0xAD
    assert pkt[3] & 0xC0 == 0xC0
    # The UTF-8 keyword body (NUL-terminated) is the trimmed "bank".
    text_region = pkt.split(b"ENU\x00", 1)[1]
    idx = text_region.find(b"bank")
    assert idx != -1
    assert text_region[idx:-1].decode("utf-8") == "bank"
