"""Tests for greeting tokenization in the tier-1 speech fast-path.

Punctuation glued to a greeting word ("Hello!", "안녕!", "hey...") must not
defeat the cheap pattern-match tier, which would otherwise force a fall-through
to the LLM (or, with no LLM, a robotic non-greeting fallback reply).
"""

from __future__ import annotations

from anima.action.speech import GREETINGS, _greeting_tokens


def _matches(text: str) -> bool:
    """Mirror the tier-1 guard in respond_to_speech."""
    words = _greeting_tokens(text)
    return bool(words & GREETINGS) and len(words) <= 3


class TestGreetingTokens:
    def test_trailing_exclamation_still_matches(self) -> None:
        assert _matches("Hello!")
        assert _matches("Hi!")

    def test_korean_greeting_with_punctuation(self) -> None:
        assert _matches("안녕!")
        assert "안녕" in _greeting_tokens("안녕!")

    def test_ellipsis_and_commas_stripped(self) -> None:
        assert _matches("hey...")
        assert _matches("Hail, friend")

    def test_plain_greeting_matches(self) -> None:
        assert _matches("hello")
        assert _matches("greetings")

    def test_long_question_does_not_match(self) -> None:
        # More than 3 tokens -> route to the LLM tier, not the greeting path.
        assert not _matches(
            "how do I get to the bank from here please"
        )

    def test_non_greeting_does_not_match(self) -> None:
        assert not _matches("selling iron ingots cheap")

    def test_inner_punctuation_preserved(self) -> None:
        # Only surrounding punctuation is stripped; word-internal stays intact.
        assert _greeting_tokens("hi-there") == {"hi-there"}

    def test_empty_and_punctuation_only(self) -> None:
        assert _greeting_tokens("") == set()
        assert _greeting_tokens("!!! ...") == set()
