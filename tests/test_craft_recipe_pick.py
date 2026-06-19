"""CraftBlacksmith._pick_recipe selection across skill regimes.

Regression for the CRAFTING worth=0 plateau: once Blacksmithy skill outgrows
the trainable band (success > 90% on everything affordable), the old code's
``max(affordable, key=success)`` tied at clamped success 1.0 and returned the
first recipe in list order — the Dagger (3 ingots, near-worthless) — so a
GM-skill smith produced trash and never converted ingots into sellable value.
The fix picks the most valuable reliably-craftable item instead.
"""
from types import SimpleNamespace

import anima.procedures.craft_blacksmith as cb
from anima.procedures.craft_blacksmith import CraftBlacksmith

BLACKSMITHY_SKILL_ID = 7


def _ctx(skill_value: float):
    skill = SimpleNamespace(id=BLACKSMITHY_SKILL_ID, value=skill_value)
    ss = SimpleNamespace(skills={BLACKSMITHY_SKILL_ID: skill})
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


def _pick(monkeypatch, skill_value: float, ingots: int):
    monkeypatch.setattr(cb, "_count_iron_ingots", lambda ctx: ingots)
    return CraftBlacksmith()._pick_recipe(_ctx(skill_value))


def test_gm_smith_picks_valuable_item_not_dagger(monkeypatch):
    # Skill 100, plenty of ingots: every recipe is near-certain. The old code
    # returned the Dagger (first in list, success ties at 1.0). The fix must
    # pick the most valuable reliable item (highest ingot cost as value proxy).
    recipe = _pick(monkeypatch, skill_value=100.0, ingots=100)
    assert recipe is not None
    name, _grp, _idx, cost = recipe
    assert name != "Dagger", "GM smith must not spam worthless daggers"
    # Ringmail Tunic (18 ingots) is the most valuable affordable recipe here.
    expected = max(cb._RECIPES, key=lambda r: r[3])  # highest ingot cost
    assert name == expected[0]
    assert cost == expected[3]


def test_high_skill_limited_ingots_picks_costliest_affordable(monkeypatch):
    # Skill 99 but only 12 ingots: the 14/16/18-ingot recipes are unaffordable.
    # Must still pick the costliest item that fits (Longsword, 12) over Dagger.
    recipe = _pick(monkeypatch, skill_value=99.0, ingots=12)
    assert recipe is not None
    name, _grp, _idx, cost = recipe
    assert name != "Dagger"
    assert cost <= 12
    affordable_costs = [r[3] for r in cb._RECIPES if r[3] <= 12]
    assert cost == max(affordable_costs)


def test_trainable_band_still_prefers_cheapest(monkeypatch):
    # Skill 50: several recipes sit in the 25-90% trainable band. Behaviour
    # there is unchanged — cheapest ingot cost wins so skill keeps climbing.
    recipe = _pick(monkeypatch, skill_value=50.0, ingots=100)
    assert recipe is not None
    name, _grp, _idx, cost = recipe
    # Compute the trainable set the way the procedure does.
    trainable = [
        (n, c, min(1.0, max(0.0, (50.0 - m) / 50.0)))
        for n, _g, _i, c, m in cb._RECIPES
    ]
    trainable = [(n, c) for n, c, s in trainable if 0.25 <= s <= 0.90]
    assert trainable, "expected some recipes in the trainable band at skill 50"
    assert cost == min(c for _n, c in trainable)


def test_below_band_maximises_success(monkeypatch):
    # Skill 20 with only 8 ingots: nothing is in or above the band, so pick the
    # highest success rate to waste the fewest ingots on failures.
    recipe = _pick(monkeypatch, skill_value=20.0, ingots=8)
    assert recipe is not None
    name, _grp, _idx, _cost = recipe
    # Dagger (minSkill -0.4) has the highest success at low skill among the
    # 8-ingot-affordable set, so it is the correct pick here (not a plateau).
    best = max(
        ((n, min(1.0, max(0.0, (20.0 - m) / 50.0))) for n, _g, _i, c, m in cb._RECIPES if c <= 8),
        key=lambda t: t[1],
    )
    assert name == best[0]


def test_no_ingots_returns_none(monkeypatch):
    assert _pick(monkeypatch, skill_value=100.0, ingots=0) is None
