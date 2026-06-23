"""Robustness: ``_parse_action`` must never raise on hallucinated LLM output.

``json.loads`` raises a BARE ``ValueError`` (not a ``json.JSONDecodeError``)
when a JSON value is a single unbroken integer longer than CPython's
int-string-conversion limit (4300 digits, raised since 3.11) — and a
``RecursionError`` on a deeply nested blob. The parser only caught
``json.JSONDecodeError``, so such a response propagated out of
``_parse_action`` and crashed the whole THINK tick instead of degrading to the
documented ``None`` (which the caller already handles). An LLM emitting a long
number in any field (a hallucinated id / coordinate / price) is real,
untrusted external input, so this is a reachable error-path crash.
"""

from __future__ import annotations

import pytest

from anima.brain.think import _parse_action

# Comfortably past CPython's 4300-digit int<->str conversion limit.
_LONG_INT = "1" * 4500


def test_bare_object_with_long_int_value_does_not_raise():
    # A top-level JSON object whose numeric field overflows the int-str limit.
    text = '{"action": "go", "place": "bank", "id": ' + _LONG_INT + "}"
    # Before the fix this raised ValueError out of json.loads; now it must
    # degrade to None (unparseable) rather than crash the tick.
    assert _parse_action(text) is None


def test_long_int_object_embedded_in_prose_does_not_raise():
    # The brace-scanner branch (a JSON object buried in free text) must also
    # survive the long-int value while still scanning for a later valid object.
    text = (
        "Sure, here is my decision: "
        '{"action": "explore", "n": ' + _LONG_INT + "}"
    )
    assert _parse_action(text) is None


def test_long_int_in_fenced_block_does_not_raise():
    # The ```json fenced-block branch must survive it too.
    text = '```json\n{"action": "idle", "k": ' + _LONG_INT + "}\n```"
    assert _parse_action(text) is None


def test_deeply_nested_json_does_not_raise():
    # A pathological deeply-nested blob trips RecursionError inside json.loads;
    # it must be swallowed like any other unparseable response.
    depth = 20000
    text = "[" * depth + "]" * depth
    assert _parse_action(text) is None


def test_good_object_after_long_int_still_parses():
    # The long-int object must not poison the scan: a VALID object later in the
    # text is still recovered (the brace scanner keeps going past the bad one).
    text = (
        '{"x": ' + _LONG_INT + "}\n"
        '{"action": "go", "place": "bank"}'
    )
    parsed = _parse_action(text)
    assert parsed is not None
    assert parsed.get("action") == "go"
    assert parsed.get("place") == "bank"


def test_happy_path_unchanged():
    # Widening the catch from JSONDecodeError to ValueError must not alter the
    # normal parse (JSONDecodeError already subclasses ValueError).
    parsed = _parse_action('{"action": "idle", "say": ""}')
    assert parsed == {"action": "idle", "say": ""}
