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
