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


class TestRecordRun:
    """record_run must only count procedures that actually ran this tick.

    Regression: the run loop used to fall back to the *previous* tick's
    procedure name on an idle tick (planner returned None), feeding the
    loop-detection window with a procedure that did not run. After window//2
    idle ticks is_looping() tripped on that phantom, which fired a 60s health
    break that reset the idle-tick escalation timer — so a genuinely idle /
    deadlocked agent ping-ponged between health breaks and never escalated to
    deadlock recovery.
    """

    def test_idle_tick_does_not_record(self):
        h = PlannerHealth(window=20, min_diversity=0.2)
        # Simulate an agent that ran one procedure then went idle: the old
        # loop replayed "mine_ore" on every idle tick.
        h.record_run("mine_ore", ran=True)
        for _ in range(40):
            h.record_run("mine_ore", ran=False)  # idle ticks: must NOT record
        # Only the single real run is in the window — nowhere near enough data
        # to call a loop, and certainly not a phantom mine_ore loop.
        assert h.is_looping() is False
        assert h.dominant_procedure() == "mine_ore"

    def test_phantom_idle_replay_would_have_looped(self):
        """Document the bug: replaying the stale name on idle DOES look like a
        loop (this is what record_run prevents)."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(20):
            h.record("mine_ore")  # the buggy path: phantom replay each idle tick
        assert h.is_looping() is True

    def test_record_run_counts_real_runs(self):
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(20):
            h.record_run("mine_ore", ran=True)
        assert h.is_looping() is True

    def test_record_run_ignores_empty_name(self):
        h = PlannerHealth(window=20)
        h.record_run("", ran=True)
        assert h.dominant_procedure() is None
