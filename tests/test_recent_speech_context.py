"""Regression test: _build_recent_speech must filter non-conversational lines
BEFORE limiting the window, and classify by msg_type — not by a name string.

Two bugs lived here:
  1. ``recent(count=3)`` was sliced FIRST, then system/label entries were
     dropped. A burst of system/cliloc/label noise in the last 3 journal slots
     therefore shut out a real player message that arrived just before it, so
     the LLM saw "no conversation" and never replied.
  2. Classification used ``entry.name.lower() == "system"``. A localized
     (cliloc) system line carrying a vendor/NPC name (e.g. name="Hastin") or a
     single-click LABEL response ("Hastin the baker") was *not* named "system",
     so it leaked into the conversational context fed to the LLM.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from anima.brain.think import _build_recent_speech
from anima.perception.enums import MessageType


def _entry(serial, name, text, msg_type=MessageType.REGULAR):
    return SimpleNamespace(
        serial=serial,
        name=name,
        text=text,
        msg_type=msg_type,
        hue=0,
        timestamp=time.time(),
    )


def _ctx(entries, my_serial=0x00000001):
    """BrainContext stand-in whose social.recent() returns the LAST `count`
    entries — matching SocialState.recent's chronological tail semantics."""

    def recent(count=10):
        return entries[-count:]

    return SimpleNamespace(
        perception=SimpleNamespace(
            social=SimpleNamespace(recent=recent),
            self_state=SimpleNamespace(serial=my_serial),
        )
    )


PLAYER = 0x00012345


def test_player_line_survives_trailing_system_noise() -> None:
    # A real player message, then three system/label lines pile up after it.
    entries = [
        _entry(PLAYER, "Aelius", "Hello there!", MessageType.REGULAR),
        _entry(0xFFFFFFFF, "System", "You see a bright flash.", MessageType.SYSTEM),
        _entry(0x00099, "Hastin", "Hastin the baker", MessageType.LABEL),
        _entry(0xFFFFFFFF, "System", "It begins to rain.", MessageType.SYSTEM),
    ]
    out = _build_recent_speech(_ctx(entries))
    # Old code sliced the last 3 (all noise) then filtered -> empty string.
    assert "Aelius" in out
    assert "Hello there!" in out
    # And the noise never reaches the LLM.
    assert "rain" not in out
    assert "baker" not in out


def test_label_with_npc_name_is_not_conversation() -> None:
    # A LABEL line carrying a real NPC name must be classified out by msg_type,
    # not slip through the name-string check.
    entries = [_entry(0x00099, "Hastin", "Hastin the baker", MessageType.LABEL)]
    assert _build_recent_speech(_ctx(entries)) == ""


def test_cliloc_system_line_with_a_name_is_dropped() -> None:
    entries = [
        _entry(0x00077, "a town crier", "Hear ye!", MessageType.SYSTEM),
    ]
    assert _build_recent_speech(_ctx(entries)) == ""


def test_window_keeps_only_the_last_three_real_lines() -> None:
    entries = [
        _entry(PLAYER, "Aelius", "one", MessageType.REGULAR),
        _entry(PLAYER, "Aelius", "two", MessageType.REGULAR),
        _entry(PLAYER, "Aelius", "three", MessageType.REGULAR),
        _entry(PLAYER, "Aelius", "four", MessageType.REGULAR),
    ]
    out = _build_recent_speech(_ctx(entries))
    assert "one" not in out
    assert "two" in out and "three" in out and "four" in out


def test_own_speech_is_attributed_to_you() -> None:
    entries = [_entry(0x00000001, "Anima", "I greet you", MessageType.REGULAR)]
    out = _build_recent_speech(_ctx(entries))
    assert 'You: "I greet you"' in out


def test_emote_and_yell_are_real_conversation() -> None:
    entries = [
        _entry(PLAYER, "Aelius", "waves", MessageType.EMOTE),
        _entry(PLAYER, "Aelius", "HELLO!", MessageType.YELL),
    ]
    out = _build_recent_speech(_ctx(entries))
    assert "waves" in out
    assert "HELLO!" in out


def test_empty_when_no_conversation() -> None:
    assert _build_recent_speech(_ctx([])) == ""
