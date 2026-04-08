"""Tests for the typed PlannerBlackboard wrapper."""
from anima.planner.state import PlannerBlackboard


class TestPlannerBlackboard:
    def test_default_construction(self):
        bb = PlannerBlackboard()
        assert bb.craft_bs_fails == 0
        assert bb.skip_procedures == set()
        assert bb.depleted_banks == {}
        assert bb.junk_ore_serials == set()

    def test_from_dict_roundtrip(self):
        """Loading from and writing to a dict preserves all fields."""
        data = {
            "_craft_bs_fails": 5,
            "_skip_procedures": {"mine_ore"},
            "depleted_banks": {(10, 20): 1234.5},
            "_junk_ore_serials": {0x4001},
        }
        bb = PlannerBlackboard.from_dict(data)
        assert bb.craft_bs_fails == 5
        assert bb.skip_procedures == {"mine_ore"}
        assert bb.depleted_banks == {(10, 20): 1234.5}
        assert bb.junk_ore_serials == {0x4001}

        out = bb.to_dict()
        assert out["_craft_bs_fails"] == 5
        assert out["_skip_procedures"] == {"mine_ore"}

    def test_from_dict_unknown_keys_preserved(self):
        """Unknown keys are kept in extras so we don't lose legacy state."""
        data = {"legacy_flag": True}
        bb = PlannerBlackboard.from_dict(data)
        assert bb.extras["legacy_flag"] is True
        out = bb.to_dict()
        assert out["legacy_flag"] is True
