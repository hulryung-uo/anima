"""Tests for randomized character creation stat/skill validity.

Modern ServUO (NewCharacterCreation) requires each stat in 10..60 and the
total to equal EXACTLY 90; an off-target total is rescaled and any stat pushed
past 60 resets the whole spread to 10/10/10. CharacterAppearance.random() must
therefore always emit a valid 90-total spread.
"""

from __future__ import annotations

import random

from anima.client.appearance import PERSONA_STATS, CharacterAppearance


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
