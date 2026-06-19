"""CraftCarpentry._pick_target selects the highest-skill feasible item.

Regression: CRAFT_TARGETS is NOT sorted by min_skill, but the old execute()
loop assumed it was and kept the *last* list-order match. That made a
high-skill carpenter craft a low-skill item (e.g. the 63.1-skill Lap Harp
instead of the 78.9-skill Shepherd's Crook at skill 80), so success was
near-certain and skill gain was near-zero — the carpentry skill-gain stall.
"""
from anima.skills.crafting.carpentry import CRAFT_TARGETS, CraftCarpentry


def _max_feasible(skill_val: float, boards: int):
    """The item with the highest min_skill the carpenter can craft now."""
    feasible = [
        t for t in CRAFT_TARGETS
        if skill_val >= t[3] and boards >= t[4]
    ]
    if not feasible:
        return None
    return max(feasible, key=lambda t: t[3])


def test_high_skill_picks_highest_skill_item_not_list_order():
    # Skill 80 with ample boards: feasible set includes Lap Harp (63.1) AND
    # Shepherd's Crook (78.9). Lap Harp comes *later* in CRAFT_TARGETS, so the
    # old "last wins" loop returned it. The fix must pick Shepherd's Crook.
    target, best_feasible = CraftCarpentry._pick_target(80.0, 100)
    assert target is not None
    name, _grp, _idx, _boards = target
    assert name == "Shepherd's Crook"
    assert name == _max_feasible(80.0, 100)[0]


def test_picks_max_feasible_across_skill_regimes():
    for skill in (0.0, 12.0, 25.0, 40.0, 55.0, 65.0, 80.0, 100.0):
        target, _ = CraftCarpentry._pick_target(skill, 100)
        expected = _max_feasible(skill, 100)
        assert expected is not None
        assert target is not None
        assert target[0] == expected[0], f"skill {skill}: got {target[0]}"


def test_board_shortage_reports_highest_skill_unaffordable_item():
    # Skill 100 but only 6 boards: the carpenter qualifies for everything, but
    # can only afford Quarter Staff (6) / Shepherd's Crook (7 -> no). With 6
    # boards, target is the highest-skill item costing <= 6, and best_feasible
    # reports the highest-skill item it cannot yet afford (Standing Harp, 35).
    target, best_feasible = CraftCarpentry._pick_target(100.0, 6)
    assert target is not None
    assert target[0] == _max_feasible(100.0, 6)[0]
    assert best_feasible is not None
    # Highest-min_skill item we qualify for but can't afford with 6 boards.
    unaffordable = [
        t for t in CRAFT_TARGETS if 100.0 >= t[3] and 6 < t[4]
    ]
    expected = max(unaffordable, key=lambda t: t[3])
    assert best_feasible == (expected[0], expected[4])


def test_nothing_craftable_returns_none_target():
    # Skill 0 with 0 boards: cannot make anything; target is None and the
    # shortage hint points at the cheapest 0-skill item we lack boards for.
    target, best_feasible = CraftCarpentry._pick_target(0.0, 0)
    assert target is None
    assert best_feasible is not None


def test_low_skill_skips_unqualified_items():
    # Skill 5 qualifies only for Barrel Staves (0.0). Even with infinite
    # boards it must not select a higher-min_skill item.
    target, _ = CraftCarpentry._pick_target(5.0, 1000)
    assert target is not None
    assert target[0] == "Barrel Staves"
