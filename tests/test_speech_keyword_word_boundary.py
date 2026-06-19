"""Word-boundary matching for the 0xAD speech keyword detector.

ServUO's NPC keyword dispatch was written against ClassicUO, whose
``SpeechesLoader.IsMatch`` only treats a keyword as present when it stands on
word boundaries (string edge / whitespace / any non-letter on each side):
"bank", " bank ", "!bank", "bank!" match, but "embankment" does not.

``_match_keywords`` previously used a raw ``phrase in text`` substring test, so
incidental substrings inside ordinary chat ("embankment", "checking",
"checkout", "rebalance") triggered keyword encoding. That flips a normal social
reply into a UTF-8 *keyword-encoded* 0xAD frame (``type |= 0xC0``), which ServUO
routes to NPC keyword dispatch instead of showing it as plain speech —
corrupting the agent's conversations. These tests pin the boundary semantics.
"""

from __future__ import annotations

from anima.client.packets import _match_keywords, build_unicode_speech


def test_substring_inside_a_word_does_not_match() -> None:
    # "bank" buried inside "embankment" must NOT be treated as the bank keyword.
    assert _match_keywords("I sat by the embankment") == []
    # "check" inside "checking"/"checkout"/"uncheck" must NOT match.
    assert _match_keywords("I am checking my gear") == []
    assert _match_keywords("Time to checkout the new vendor") == []
    assert _match_keywords("Please uncheck that") == []
    # "balance" inside "rebalance" must NOT match.
    assert _match_keywords("rebalance the economy") == []


def test_standalone_keywords_still_match() -> None:
    # 0x2 = bank
    assert _match_keywords("I went to the bank today") == [0x0002]
    # 0x1 = balance, 0x3 = check (both stand on word boundaries here)
    assert _match_keywords("check my balance") == [0x0001, 0x0003]
    # A bare keyword is the whole line.
    assert _match_keywords("bank") == [0x0002]


def test_punctuation_counts_as_a_boundary() -> None:
    # ClassicUO treats leading/trailing non-letters as boundaries.
    assert _match_keywords("say bank!") == [0x0002]
    assert _match_keywords("...bank...") == [0x0002]


def test_multiword_keyword_phrase_matches() -> None:
    # "vendor sell" (0x14D) is a two-word phrase; the trailing boundary is the
    # space before "my", which is a non-letter, so it must still match.
    assert _match_keywords("vendor sell my loot") == [0x014D]


def test_casual_reply_is_sent_as_plain_unicode_not_keyword_encoded() -> None:
    # End-to-end: a social reply that merely *contains* a keyword substring must
    # ship as a plain UTF-16BE 0xAD frame (no Encoded 0xC0 bit), so other
    # players see it as ordinary speech instead of it being eaten by NPC
    # keyword dispatch.
    pkt = build_unicode_speech("I am just checking out the embankment")
    assert pkt[0] == 0xAD
    msg_type = pkt[3]
    assert msg_type & 0xC0 == 0, "casual chat must not set the Encoded bit"
    # Plain frame body is UTF-16BE terminated by a u16 NUL.
    assert pkt.endswith(b"\x00\x00")


def test_genuine_bank_command_is_keyword_encoded() -> None:
    # A real standalone "bank" command must still encode so the banker fires.
    pkt = build_unicode_speech("bank")
    assert pkt[0] == 0xAD
    assert pkt[3] & 0xC0 == 0xC0, "standalone keyword must set the Encoded bit"
