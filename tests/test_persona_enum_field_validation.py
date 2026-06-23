"""Persona loader: enum-like fields (combat_disposition / profession) must be
validated at load time so a typo can't silently propagate into the planner.

The active combat gate (anima/procedures/combat_loop.py:_initiates_combat)
treats anything that is not exactly ``"pacifist"`` as "may start fights", and
the planner picks its procedure chain via ``PROFESSION_LOOPS.get(profession)``.
So a misspelled value never crashes — it just does the wrong thing. These tests
pin that an unknown value is reset to the documented default at the YAML
boundary while valid values pass through untouched.
"""
from pathlib import Path

import pytest

from anima.persona import Persona, load_persona


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "p.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_typo_combat_disposition_resets_to_default(tmp_path):
    # "pacafist" is NOT exactly "pacifist", so without validation the combat
    # gate would let this persona hunt despite the author meaning otherwise.
    path = _write(
        tmp_path,
        "name: Typo\ncombat_disposition: pacafist\nprofession: bard\n",
    )
    p = load_persona(path)
    assert p.combat_disposition == "defensive"
    # profession was valid and must be preserved.
    assert p.profession == "bard"


def test_typo_profession_resets_to_empty(tmp_path):
    # "blacksmth" misses every PROFESSION_LOOPS key -> silent mining fallthrough.
    path = _write(
        tmp_path,
        "name: Typo\ncombat_disposition: aggressive\nprofession: blacksmth\n",
    )
    p = load_persona(path)
    assert p.profession == ""
    assert p.combat_disposition == "aggressive"


@pytest.mark.parametrize("disp", ["aggressive", "defensive", "pacifist"])
def test_valid_combat_disposition_preserved(tmp_path, disp):
    path = _write(tmp_path, f"name: Ok\ncombat_disposition: {disp}\n")
    assert load_persona(path).combat_disposition == disp


@pytest.mark.parametrize(
    "prof", ["", "mage", "bard", "thief", "adventurer", "blacksmith"]
)
def test_valid_profession_preserved(tmp_path, prof):
    # An empty profession is written as an explicit empty string.
    body = "name: Ok\n" + (f"profession: {prof}\n" if prof else 'profession: ""\n')
    assert load_persona(path=_write(tmp_path, body)).profession == prof


def test_missing_fields_keep_dataclass_defaults(tmp_path):
    # A persona that omits both fields entirely must still be valid (the
    # dataclass defaults — "defensive" / "" — are themselves valid values).
    path = _write(tmp_path, "name: Bare\n")
    p = load_persona(path)
    assert p.combat_disposition == "defensive"
    assert p.profession == ""


def test_pacifist_typo_would_otherwise_hunt(tmp_path):
    """Regression guard tying the loader fix to the consumer.

    The planner's gate only special-cases the exact string "pacifist"; the
    raw typo "pacafist" would compare unequal and hunt. After load-time
    validation the field is a known value, so the gate behaves predictably.
    """
    raw_typo = "pacafist"
    # Demonstrate the latent hazard on the raw (unvalidated) value...
    assert (raw_typo != "pacifist") is True  # would be treated as "may fight"
    # ...and that the loader neutralizes it to a known-valid value.
    path = _write(tmp_path, f"name: T\ncombat_disposition: {raw_typo}\n")
    assert load_persona(path).combat_disposition in {
        "aggressive",
        "defensive",
        "pacifist",
    }
