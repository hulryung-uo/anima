"""Tests for Character Markdown loader (Step 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from anima.character.loader import Character, load_character, parse_character


SAMPLE_MD = """\
# TestChar

A brave adventurer from Britain. Loves mining.

## Skills

### Primary
- Mining: target 100, priority 1
- Blacksmithy: target 80, priority 2

### Support
- Tinkering: target 60

## Activities

- Mining: 60%
- Crafting: 25%
- Selling: 15%

## Locations

- Home: Britain
- Mine: Britain North Mine
- Bank: Britain Bank

## Economy

### Keep
- pickaxe, tongs

### Sell
- swords, armor

### Thresholds
- Min gold reserve: 500
- Bank when carrying 200+ ingots
"""


class TestParseCharacter:
    def test_name(self):
        char = parse_character(SAMPLE_MD)
        assert char.name == "TestChar"

    def test_description(self):
        char = parse_character(SAMPLE_MD)
        assert "brave adventurer" in char.description
        assert "mining" in char.description.lower()

    def test_skills(self):
        char = parse_character(SAMPLE_MD)
        assert len(char.skills) == 3
        mining = char.skills[0]
        assert mining.name == "Mining"
        assert mining.target == 100.0
        assert mining.priority == 1

        blacksmithy = char.skills[1]
        assert blacksmithy.target == 80.0
        assert blacksmithy.priority == 2

        tinkering = char.skills[2]
        assert tinkering.name == "Tinkering"
        assert tinkering.target == 60.0
        assert tinkering.priority == 2  # support → priority 2

    def test_activities(self):
        char = parse_character(SAMPLE_MD)
        assert len(char.activities) == 3
        assert char.activities[0].name == "Mining"
        assert char.activities[0].weight == pytest.approx(0.6)
        assert char.activities[1].weight == pytest.approx(0.25)

    def test_locations(self):
        char = parse_character(SAMPLE_MD)
        assert char.locations["Home"] == "Britain"
        assert char.locations["Mine"] == "Britain North Mine"
        assert char.locations["Bank"] == "Britain Bank"

    def test_economy_keep(self):
        char = parse_character(SAMPLE_MD)
        assert "pickaxe" in char.keep_items
        assert "tongs" in char.keep_items

    def test_economy_sell(self):
        char = parse_character(SAMPLE_MD)
        assert "swords" in char.sell_items
        assert "armor" in char.sell_items

    def test_thresholds(self):
        char = parse_character(SAMPLE_MD)
        assert char.thresholds["Min gold reserve"] == 500

    def test_raw_markdown_preserved(self):
        char = parse_character(SAMPLE_MD)
        assert "# TestChar" in char.raw_markdown


class TestLoadCharacter:
    def test_load_hully(self):
        path = Path(__file__).parent.parent / "characters" / "hully.md"
        if not path.exists():
            pytest.skip("hully.md not found")
        char = load_character(path)
        assert char.name == "Hully"
        assert len(char.skills) >= 2
        assert any(s.name == "Mining" for s in char.skills)
        assert char.locations.get("Home") == "Minoc"

    def test_load_grimm(self):
        path = Path(__file__).parent.parent / "characters" / "grimm.md"
        if not path.exists():
            pytest.skip("grimm.md not found")
        char = load_character(path)
        assert char.name == "Grimm"
        assert any(s.name == "Lumberjacking" for s in char.skills)


class TestEdgeCases:
    def test_minimal_markdown(self):
        char = parse_character("# MinChar\n\nJust a name.")
        assert char.name == "MinChar"
        assert char.description == "Just a name."
        assert char.skills == []

    def test_missing_sections(self):
        md = "# Partial\n\nSome description.\n\n## Skills\n\n### Primary\n- Mining: target 50"
        char = parse_character(md)
        assert char.name == "Partial"
        assert len(char.skills) == 1
        assert char.locations == {}

    def test_empty_input(self):
        char = parse_character("")
        assert char.name == ""
