"""Tests for randomized character creation stat/skill validity.

Modern ServUO (NewCharacterCreation) requires each stat in 10..60 and the
total to equal EXACTLY 90; an off-target total is rescaled and any stat pushed
past 60 resets the whole spread to 10/10/10. CharacterAppearance.random() must
therefore always emit a valid 90-total spread.
"""

from __future__ import annotations

import random

from anima.client.appearance import (
    _DEFAULT_PERSONA,
    PERSONA_SKILLS,
    PERSONA_STATS,
    CharacterAppearance,
)


class TestRandomStats:
    def test_total_is_90_and_each_in_range(self) -> None:
        # Exercise the full strength range so the int=remainder edge cases
        # (small strength -> large intelligence) are covered.
        random.seed(1234)
        for _ in range(5000):
            app = CharacterAppearance.random()
            stats = (app.strength, app.dexterity, app.intelligence)
            assert sum(stats) == 90, f"stat total must be 90, got {stats}"
            for s in stats:
                assert 10 <= s <= 60, f"stat out of 10..60 range: {stats}"

    def test_not_legacy_80_total(self) -> None:
        # Guard against regressing to the legacy 80-total spread that ServUO
        # silently reset to 10/10/10.
        random.seed(7)
        app = CharacterAppearance.random()
        assert app.strength + app.dexterity + app.intelligence != 80


class TestPersonaStats:
    def test_all_personas_total_90_in_range(self) -> None:
        for name, (s, d, i) in PERSONA_STATS.items():
            assert s + d + i == 90, f"{name} stats must total 90, got {(s, d, i)}"
            for v in (s, d, i):
                assert 10 <= v <= 60, f"{name} stat out of range: {(s, d, i)}"

    def test_from_persona_uses_valid_stats(self) -> None:
        random.seed(99)
        for name in PERSONA_STATS:
            app = CharacterAppearance.from_persona(name)
            total = app.strength + app.dexterity + app.intelligence
            assert total == 90, f"{name}: from_persona total {total} != 90"


class TestUnknownPersonaFallback:
    """An unknown persona name must resolve to a known-good persona, not the
    arbitrary dataclass-default Alchemy+Anatomy skill set."""

    def test_default_persona_is_a_real_persona(self) -> None:
        assert _DEFAULT_PERSONA in PERSONA_STATS
        assert _DEFAULT_PERSONA in PERSONA_SKILLS

    def test_unknown_persona_uses_fallback_stats_and_skills(self) -> None:
        random.seed(2024)
        app = CharacterAppearance.from_persona("totally-bogus-typo")
        # Stats and skills must match the known-good fallback persona exactly,
        # NOT the random/default dataclass values.
        assert (
            app.strength,
            app.dexterity,
            app.intelligence,
        ) == PERSONA_STATS[_DEFAULT_PERSONA]
        assert app.skills == list(PERSONA_SKILLS[_DEFAULT_PERSONA])

    def test_unknown_persona_does_not_keep_default_skills(self) -> None:
        # The dataclass default skill set (Alchemy 50 + Anatomy 50) must never
        # leak through for an unknown persona.
        default_skills = CharacterAppearance().skills
        app = CharacterAppearance.from_persona("nope-not-a-persona")
        assert app.skills != default_skills

    def test_unknown_persona_skill_total_is_100(self) -> None:
        # ServUO ValidSkills() silently drops the whole skill set + starter
        # items unless the values sum to EXACTLY 100 (or 120).
        app = CharacterAppearance.from_persona("does-not-exist")
        assert sum(v for _, v in app.skills) in (100, 120)

    def test_known_persona_unaffected(self) -> None:
        # Regression guard: a valid persona name still maps to itself.
        app = CharacterAppearance.from_persona("mage")
        assert app.skills == list(PERSONA_SKILLS["mage"])
        assert (
            app.strength,
            app.dexterity,
            app.intelligence,
        ) == PERSONA_STATS["mage"]
