"""Tests for planner health metrics."""
from anima.planner.health import PlannerHealth


class TestPlannerHealth:
    def test_initial_state_is_healthy(self):
        h = PlannerHealth(window=20)
        assert h.is_looping() is False

    def test_low_diversity_detected(self):
        """20 selections of the same procedure → loop detected."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(20):
            h.record("mine_ore")
        assert h.is_looping() is True
        assert h.dominant_procedure() == "mine_ore"

    def test_normal_rotation_not_looping(self):
        """A healthy mine→smelt→craft→sell rotation is not a loop."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(5):
            for p in ("mine_ore", "smelt_ore", "craft_blacksmith", "sell_to_vendor"):
                h.record(p)
        assert h.is_looping() is False

    def test_window_slides(self):
        """Old entries drop off the window."""
        h = PlannerHealth(window=5, min_diversity=0.5)
        for _ in range(5):
            h.record("mine_ore")
        assert h.is_looping() is True
        # Record 5 new diverse entries — loop should clear
        for p in ("a", "b", "c", "d", "e"):
            h.record(p)
        assert h.is_looping() is False

    def test_not_enough_data_not_looping(self):
        """Need at least window//2 entries before reporting a loop."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        h.record("mine_ore")
        h.record("mine_ore")
        assert h.is_looping() is False  # only 2 entries, not enough

    def test_dominant_procedure_none_when_empty(self):
        h = PlannerHealth(window=20)
        assert h.dominant_procedure() is None

    def test_record_skip_doesnt_count(self):
        """record_skip tracks skips separately without polluting diversity."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for p in ("a", "b", "c", "d"):
            h.record(p)
        for _ in range(50):
            h.record_skip("mine_ore")  # doesn't affect is_looping
        assert h.is_looping() is False
