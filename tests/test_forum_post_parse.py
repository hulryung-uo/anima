"""Tests for forum post TITLE:/BODY: parsing.

Regression: when the LLM emits a ``TITLE:`` line but omits the ``BODY:``
marker (a very common output shape), the raw ``TITLE: ...`` scaffolding line
used to leak into the posted body and duplicate the title. The parsers must
strip the title line and use the remaining lines as the body.
"""

from __future__ import annotations

from anima.skills.forum_action import _parse_forum_post
from anima.skills.forum_skill import _parse_post


def test_parse_post_strips_title_line_when_body_marker_missing() -> None:
    raw = "TITLE: My Big Day\nToday I mined a lot of ore and sold it."
    title, body = _parse_post(raw, "Bjorn")
    assert title == "My Big Day"
    # The literal "TITLE:" scaffolding must NOT appear in the posted body.
    assert "TITLE:" not in body
    assert body == "Today I mined a lot of ore and sold it."


def test_parse_forum_post_strips_title_line_when_body_marker_missing() -> None:
    raw = "TITLE: My Big Day\nToday I mined a lot of ore and sold it."
    title, body = _parse_forum_post(raw, "Bjorn")
    assert title == "My Big Day"
    assert "TITLE:" not in body
    assert body == "Today I mined a lot of ore and sold it."


def test_parse_post_normal_title_body_unchanged() -> None:
    raw = "TITLE: My Day\nBODY:\nI did things."
    title, body = _parse_post(raw, "Bjorn")
    assert title == "My Day"
    assert body == "I did things."


def test_parse_forum_post_normal_title_body_unchanged() -> None:
    raw = "TITLE: My Day\nBODY:\nI did things."
    title, body = _parse_forum_post(raw, "Bjorn")
    assert title == "My Day"
    assert body == "I did things."


def test_parse_post_inline_body_unchanged() -> None:
    raw = "TITLE: My Day\nBODY: it begins here\nand continues."
    title, body = _parse_post(raw, "Bjorn")
    assert title == "My Day"
    assert body == "it begins here\nand continues."


def test_parse_post_no_markers_uses_fallback_title() -> None:
    raw = "Just some prose with no markers at all."
    title, body = _parse_post(raw, "Bjorn")
    assert title == "Bjorn's Post"
    assert body == "Just some prose with no markers at all."


def test_parse_forum_post_no_markers_uses_fallback_title() -> None:
    raw = "Just some prose with no markers at all."
    title, body = _parse_forum_post(raw, "Bjorn")
    assert title == "Bjorn's Adventures"
    assert body == "Just some prose with no markers at all."
