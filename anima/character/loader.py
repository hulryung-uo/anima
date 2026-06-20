"""Markdown character definition parser.

Parses character markdown files into structured Character dataclasses.
The markdown is also directly injectable into LLM prompts.

Format:
    # CharacterName

    Description paragraph.

    ## Skills
    ### Primary
    - Mining: target 100, priority 1

    ## Activities
    - Mining: 60%

    ## Locations
    - Home: Minoc

    ## Economy
    ### Keep
    - pickaxe, tongs
    ### Sell
    - crafted weapons
    ### Thresholds
    - Min gold reserve: 500
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()


@dataclass
class SkillGoal:
    name: str
    target: float = 100.0
    priority: int = 1


@dataclass
class Activity:
    name: str
    weight: float  # 0.0 - 1.0


@dataclass
class Character:
    name: str
    description: str = ""
    skills: list[SkillGoal] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    locations: dict[str, str] = field(default_factory=dict)
    keep_items: list[str] = field(default_factory=list)
    sell_items: list[str] = field(default_factory=list)
    thresholds: dict[str, int] = field(default_factory=dict)
    raw_markdown: str = ""  # original text for LLM injection


def load_character(path: Path | str) -> Character:
    """Parse a character markdown file into a Character dataclass."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_character(text)


def parse_character(text: str) -> Character:
    """Parse character markdown text into a Character dataclass."""
    lines = text.strip().split("\n")
    char = Character(name="", raw_markdown=text)

    current_h2 = ""
    current_h3 = ""
    description_lines: list[str] = []
    found_name = False

    for line in lines:
        stripped = line.strip()

        # H1 = character name
        if stripped.startswith("# ") and not stripped.startswith("## "):
            char.name = stripped[2:].strip()
            found_name = True
            continue

        # H2 sections
        if stripped.startswith("## "):
            current_h2 = stripped[3:].strip().lower()
            current_h3 = ""
            continue

        # H3 subsections
        if stripped.startswith("### "):
            current_h3 = stripped[4:].strip().lower()
            continue

        # Blank lines
        if not stripped:
            continue

        # Description: text before first ## heading
        if found_name and not current_h2:
            description_lines.append(stripped)
            continue

        # List items
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            _parse_list_item(char, current_h2, current_h3, item_text)

    char.description = " ".join(description_lines)
    return char


def _parse_list_item(char: Character, h2: str, h3: str, text: str) -> None:
    """Parse a single list item based on its section context."""
    if h2 == "skills":
        _parse_skill(char, h3, text)
    elif h2 == "activities":
        _parse_activity(char, text)
    elif h2 == "locations":
        _parse_location(char, text)
    elif h2 == "economy":
        if h3 == "keep":
            char.keep_items.extend(s.strip() for s in text.split(","))
        elif h3 == "sell":
            char.sell_items.extend(s.strip() for s in text.split(","))
        elif h3 == "thresholds":
            _parse_threshold(char, text)


def _parse_skill(char: Character, subsection: str, text: str) -> None:
    """Parse: 'Mining: target 100, priority 1'"""
    parts = text.split(":")
    if len(parts) < 2:
        char.skills.append(SkillGoal(name=text))
        return

    name = parts[0].strip()
    rest = parts[1].strip()
    target = 100.0
    priority = 2 if subsection == "support" else 1

    target_match = re.search(r"target\s+(\d+(?:\.\d+)?)", rest)
    if target_match:
        target = float(target_match.group(1))

    prio_match = re.search(r"priority\s+(\d+)", rest)
    if prio_match:
        priority = int(prio_match.group(1))

    char.skills.append(SkillGoal(name=name, target=target, priority=priority))


def _parse_activity(char: Character, text: str) -> None:
    """Parse: 'Mining: 60%' or 'Mining: 60% of time'"""
    parts = text.split(":")
    if len(parts) < 2:
        return
    name = parts[0].strip()
    rest = parts[1].strip()

    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", rest)
    if pct_match:
        weight = float(pct_match.group(1)) / 100.0
    else:
        weight = 0.1  # default

    char.activities.append(Activity(name=name, weight=weight))


def _parse_location(char: Character, text: str) -> None:
    """Parse: 'Home: Minoc'"""
    parts = text.split(":", 1)
    if len(parts) == 2:
        key = parts[0].strip()
        value = parts[1].strip()
        char.locations[key] = value


def _parse_threshold(char: Character, text: str) -> None:
    """Parse a threshold list item into ``thresholds[key] = int``.

    Handles two real shapes that appear in the shipped character files:

    * the ``key: value`` form — ``"Min gold reserve: 500"`` (and unit/comma
      decorated values like ``"Min gold reserve: 1,000 gold"``), and
    * the colonless natural-language form — ``"Bank when carrying 200+ ingots"``
      (grimm.md / hully.md both ship a ``Bank when carrying N+ ...`` line).

    The previous ``rsplit(":", 1)`` + ``int(...replace("+", ""))`` only handled
    a bare integer after a colon: a colonless line produced a 1-element split
    and was silently dropped, and a colon value carrying a unit/comma
    (``"1,000 gold"``) raised ``ValueError`` and was likewise dropped. Both
    forms therefore lost the threshold entirely. Extract the first integer
    token (commas stripped) and derive the key from the remaining text.
    """
    parts = text.rsplit(":", 1)
    if len(parts) == 2:
        key = parts[0].strip()
        m = re.search(r"(\d[\d,]*)", parts[1])
        if key and m:
            char.thresholds[key] = int(m.group(1).replace(",", ""))
        return

    # Colonless form: pull the first integer and use the surrounding words as
    # the key, e.g. "Bank when carrying 200+ ingots" -> {"Bank when carrying ingots": 200}.
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return
    value = int(m.group(1).replace(",", ""))
    key = (text[: m.start()] + text[m.end():]).replace("+", "")
    key = re.sub(r"\s+", " ", key).strip(" :,")
    if key:
        char.thresholds[key] = value
