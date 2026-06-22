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

    def test_failure_while_open_does_not_extend_cooldown(self):
        """A failure landing inside the cooldown must NOT reset the window.

        Regression: ``record_failure`` stamped ``_tripped_at`` on *every*
        failure once ``count >= max_failures``. A target that keeps failing
        while already open (a directly fetched survival heal interrupted each
        tick, or a watchdog-cancelled procedure re-blamed) therefore reset its
        own cooldown on every attempt and never reached the half-open recovery
        probe — it stayed open far past ``cooldown_s``, effectively forever.
        """
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.12)
        cb.record_failure("a")          # trips, window starts now
        assert cb.is_open("a") is True
        time.sleep(0.08)                # still inside the cooldown window
        cb.record_failure("a")          # must NOT push the window forward
        assert cb.is_open("a") is True  # still open (correctly)
        time.sleep(0.06)                # original window (0.12s) has now lapsed
        # With the bug, the second failure would have reset the clock and this
        # would still report open; with the fix the original window governs.
        assert cb.is_open("a") is False
        assert cb.is_half_open("a") is True

    def test_lapsed_unpolled_failure_still_refreshes_window(self):
        """A failure after the window lapsed (no poll) DOES re-trip fresh.

        Counterpart to the no-extend rule: when the cooldown has genuinely
        elapsed but no ``is_open`` poll armed the half-open probe, the next
        failure must re-stamp the window so the breaker re-opens (matches
        ``test_lapsed_failure_before_poll_still_retrips``).
        """
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)                # window lapsed, but no is_open() poll
        cb.record_failure("a")          # re-trip with a fresh window
        assert cb.is_open("a") is True


class TestCircuitBreakerNoLeak:
    """The breaker must not accumulate one permanent entry per distinct key.

    Regression: keys for ephemeral targets (depleted 8x8 ore-bank cells the
    miner roams away from, one-shot ore-pile serials that despawn) were tripped
    once and never revisited. The half-open eviction in ``is_open`` only fires
    when that exact key is polled again post-cooldown — which never happens for
    a key gone from the world — so ``_counts``/``_tripped_at`` grew without
    bound across a long session. Mutations now opportunistically sweep targets
    idle past the cooldown window.
    """

    def _size(self, cb: CircuitBreaker) -> int:
        # Total distinct keys the breaker is holding state for.
        return len(set(cb._counts) | set(cb._tripped_at) | set(cb._half_open))

    def test_idle_tripped_keys_are_swept_on_next_mutation(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        # Trip 100 distinct, never-revisited keys (e.g. roamed-past ore banks).
        for i in range(100):
            cb.trip(("bank", i))
        assert self._size(cb) == 100
        time.sleep(0.06)  # every key's cooldown window has now lapsed
        # A single later, unrelated trip sweeps all the lapsed, idle keys.
        cb.trip(("bank", 9999))
        # Only the just-touched key remains — not 101.
        assert self._size(cb) == 1
        assert cb.is_open(("bank", 9999)) is True

    def test_explicit_prune_expired_bounds_memory(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        for i in range(50):
            cb.trip(i)
        assert self._size(cb) == 50
        time.sleep(0.06)
        evicted = cb.prune_expired()
        assert evicted == 50
        assert self._size(cb) == 0

    def test_sweep_never_evicts_a_currently_open_target(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=10.0)
        cb.trip("hot")          # open, inside its long cooldown
        cb.trip("also_hot")
        assert cb.is_open("hot") is True
        cb.record_failure("third")  # mutation triggers a sweep
        # Nothing lapsed yet, so the open targets survive the sweep.
        assert cb.is_open("hot") is True
        assert cb.is_open("also_hot") is True

    def test_lapsed_unpolled_failure_still_retrips_despite_sweep(self):
        """The sweep must not break the fresh-window re-trip of the live key.

        The key being recorded is ``exclude``-protected, so a failure landing
        after its window lapsed (with no intervening poll) still re-opens it.
        """
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)            # window lapsed, no is_open() poll
        cb.record_failure("a")      # sweep runs, but "a" is excluded
        assert cb.is_open("a") is True

    def test_prune_keeps_recent_subthreshold_counters(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=10.0)
        cb.record_failure("a")  # 1 of 3, recent — must survive
        cb.record_failure("b")  # triggers a sweep; "a" is recent, kept
        assert cb.failure_count("a") == 1
        assert cb.failure_count("b") == 1
