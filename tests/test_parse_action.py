"""Robustness tests for the LLM think-response parser.

The think tick calls ``action.get(...)`` on whatever ``_parse_action`` returns,
so the parser must only ever yield a ``dict`` or ``None`` — never a JSON list
or scalar, which would raise ``AttributeError`` and crash the whole tick.
"""

from __future__ import annotations

import pytest

from anima.brain.think import _parse_action


def test_plain_object():
    out = _parse_action('{"action": "go", "place": "bank"}')
    assert out == {"action": "go", "place": "bank"}


def test_list_wrapped_object_unwrapped():
    # Models commonly wrap the single action in a JSON array.
    out = _parse_action('[{"action": "idle", "say": ""}]')
    assert out == {"action": "idle", "say": ""}


def test_list_wrapped_in_fenced_block():
    text = '```json\n[{"action": "explore", "reason": "scouting"}]\n```'
    out = _parse_action(text)
    assert out == {"action": "explore", "reason": "scouting"}


@pytest.mark.parametrize("text", ['"idle"', "42", "true", "null", "[]", "[1, 2, 3]"])
def test_scalar_and_non_dict_collapse_to_none(text):
    # A bare scalar / empty or dict-free list must NOT be returned as-is.
    out = _parse_action(text)
    assert out is None


def test_non_dict_result_is_safe_to_get():
    # The real failure mode: caller does action.get("action", ...).
    # Whatever the parser returns for a list-wrapped reply must support .get.
    out = _parse_action('[{"action": "speak", "say": "hi"}]')
    assert out is not None
    assert out.get("action") == "speak"


def test_garbage_returns_none():
    assert _parse_action("this is not json at all") is None


def test_embedded_object_in_prose():
    text = 'Sure! Here is my decision: {"action": "go", "place": "mine"} ok?'
    out = _parse_action(text)
    assert out == {"action": "go", "place": "mine"}


def test_embedded_object_with_brace_inside_string_value():
    # The LLM wraps JSON in prose AND puts a closing brace / emoticon inside a
    # free-text value ("say"). A brace-counting scanner that ignores string
    # literals closes the object early at the in-string '}', fails to parse the
    # truncated fragment, and drops the whole decision (agent goes mute / no-op
    # for the tick). The scanner must treat in-string braces as ordinary text.
    text = 'Decision: {"action": "speak", "say": "hi :} friend"} done'
    out = _parse_action(text)
    assert out == {"action": "speak", "say": "hi :} friend"}


def test_embedded_object_with_balanced_braces_in_string():
    text = 'Here: {"action": "speak", "say": "use {x} now"} ok'
    out = _parse_action(text)
    assert out == {"action": "speak", "say": "use {x} now"}


def test_escaped_quote_in_string_value():
    # A backslash-escaped quote inside the value must not be read as the end of
    # the string (which would then mis-track braces after it).
    text = r'note {"action": "speak", "say": "he said \"hi :}\" loudly"} end'
    out = _parse_action(text)
    assert out == {"action": "speak", "say": 'he said "hi :}" loudly'}


def test_first_brace_fragment_is_not_object_but_later_one_is():
    # A leading '{' that is not a real object (here, a brace inside prose with
    # no valid JSON) must not abort the scan: a valid object follows it.
    text = 'maybe {not json} but really {"action": "idle", "say": ""}'
    out = _parse_action(text)
    assert out == {"action": "idle", "say": ""}
