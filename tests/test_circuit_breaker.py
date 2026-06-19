"""Tests for the CircuitBreaker abstraction."""
import time
from anima.planner.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        assert cb.is_open("x") is False

    def test_opens_after_max_failures(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        assert cb.is_open("target_a") is False
        cb.record_failure("target_a")
        cb.record_failure("target_a")
        assert cb.is_open("target_a") is False  # still below threshold
        cb.record_failure("target_a")
        assert cb.is_open("target_a") is True

    def test_different_targets_independent(self):
        cb = CircuitBreaker(max_failures=2, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is True
        assert cb.is_open("b") is False

    def test_cooldown_expires(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)
        assert cb.is_open("a") is False  # cooldown expired

    def test_record_success_resets(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("a")
        cb.record_success("a")
        assert cb.failure_count("a") == 0
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is False  # needed 3 after reset

    def test_open_targets_lists_active(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("b")
        assert set(cb.open_targets()) == {"a", "b"}

    def test_trip_once_opens_immediately(self):
        """trip() skips counting and opens the breaker right away."""
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        cb.trip("a")
        assert cb.is_open("a") is True

    def test_reset_target(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        cb.reset("a")
        assert cb.is_open("a") is False

    def test_reset_all(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("b")
        cb.reset_all()
        assert cb.is_open("a") is False
        assert cb.is_open("b") is False

    def test_hashable_targets(self):
        """Targets can be tuples, ints, strings — anything hashable."""
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure((10, 20))
        cb.record_failure(42)
        cb.record_failure("name")
        assert cb.is_open((10, 20)) is True
        assert cb.is_open(42) is True
        assert cb.is_open("name") is True

    def test_half_open_after_cooldown(self):
        """After cooldown the target is probed once (half-open)."""
        cb = CircuitBreaker(max_failures=2, cooldown_s=0.05)
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is True
        assert cb.is_half_open("a") is False
        time.sleep(0.06)
        # Evaluating is_open after cooldown lapses puts it half-open.
        assert cb.is_open("a") is False
        assert cb.is_half_open("a") is True
        assert cb.failure_count("a") == 0

    def test_half_open_failure_retrips_immediately(self):
        """A failing probe re-opens at once, not after max_failures again."""
        cb = CircuitBreaker(max_failures=3, cooldown_s=0.05)
        for _ in range(3):
            cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)
        assert cb.is_open("a") is False  # cooldown lapsed -> half-open probe
        assert cb.is_half_open("a") is True
        # The single probe attempt fails: known-bad target re-trips now,
        # without having to burn max_failures (3) attempts all over again.
        cb.record_failure("a")
        assert cb.is_open("a") is True
        assert cb.is_half_open("a") is False

    def test_half_open_success_clears(self):
        """A successful probe fully recovers the target (no half-open left)."""
        cb = CircuitBreaker(max_failures=2, cooldown_s=0.05)
        cb.record_failure("a")
        cb.record_failure("a")
        time.sleep(0.06)
        assert cb.is_open("a") is False
        assert cb.is_half_open("a") is True
        cb.record_success("a")
        assert cb.is_half_open("a") is False
        assert cb.failure_count("a") == 0
        # Back to a clean slate: needs a full max_failures run to re-open.
        cb.record_failure("a")
        assert cb.is_open("a") is False

    def test_reset_clears_half_open(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        time.sleep(0.06)
        assert cb.is_open("a") is False  # -> half-open
        assert cb.is_half_open("a") is True
        cb.reset("a")
        assert cb.is_half_open("a") is False

    def test_snapshot_does_not_mutate_into_half_open(self):
        """Logging a snapshot must not consume the half-open probe slot.

        Regression: snapshot()/open_targets() routed through is_open(),
        whose cooldown-lapse branch mutates state (clears the counter and
        flips the target half-open). A pure diagnostic read silently armed
        the one-shot probe and zeroed failure_count before any real gating
        poll, so the next failure re-counted from zero instead of re-tripping.
        """
        cb = CircuitBreaker(max_failures=2, cooldown_s=0.05)
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)  # cooldown lapses, but no is_open() poll yet

        snap = cb.snapshot()
        # Read must not have flipped the target half-open or cleared its count.
        assert cb.is_half_open("a") is False
        assert cb.failure_count("a") == 2
        # snapshot still correctly reports the lapsed target as no-longer-open.
        assert snap["open"] == []

        # The real gating poll is what arms the half-open probe.
        assert cb.is_open("a") is False
        assert cb.is_half_open("a") is True

    def test_open_targets_is_pure(self):
        """open_targets() must not transition lapsed targets to half-open."""
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        time.sleep(0.06)
        assert cb.open_targets() == []  # lapsed -> not open
        # ...but the read left the failure bookkeeping intact (no half-open).
        assert cb.is_half_open("a") is False
        assert cb.failure_count("a") == 1

    def test_lapsed_failure_before_poll_still_retrips(self):
        """A failure arriving after lapse (no poll) re-trips with a fresh window.

        Because diagnostic reads no longer pre-arm half-open, the counter is
        still at max when the failure lands, so the breaker stays open.
        """
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        time.sleep(0.06)
        cb.snapshot()  # pure read, must not disturb state
        cb.record_failure("a")
        assert cb.is_open("a") is True
