"""Tests for UO-themed name generators."""

from __future__ import annotations

import re

from anima.naming import generate_account_name, generate_character_name


class TestAccountName:
    def test_format(self) -> None:
        name = generate_account_name()
        # Word + 4 digits
        assert re.match(r"^[A-Z][a-z]+\d{4}$", name), f"Bad format: {name}"

    def test_unique(self) -> None:
        names = {generate_account_name() for _ in range(20)}
        # With 34 words * 9000 digits, collisions are very unlikely
        assert len(names) >= 15


class TestCharacterName:
    def test_two_parts(self) -> None:
        name = generate_character_name()
        # Should have at least 2 words
        assert " " in name, f"Expected two-word name: {name}"
        parts = name.split(" ", 1)
        assert len(parts[0]) >= 2
        assert len(parts[1]) >= 2

    def test_unique(self) -> None:
        names = {generate_character_name() for _ in range(20)}
        assert len(names) >= 15

    def test_reasonable_length(self) -> None:
        for _ in range(50):
            name = generate_character_name()
            assert len(name) <= 30, f"Name too long: {name}"
