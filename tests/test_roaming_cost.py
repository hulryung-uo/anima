"""Tests for LocationScore and cost-weighted location selection."""
import time
from unittest.mock import MagicMock

import pytest

from anima.planner.roaming import LocationScore, LocationStats
from anima.world_knowledge import Location


def _loc(name: str, x: int = 100, y: int = 100) -> Location:
    return Location(name, x, y, "test")


class TestLocationScore:
    def test_score_decreases_with_distance(self):
        s_near = LocationScore(_loc("A"), distance=10).score()
        s_far = LocationScore(_loc("B"), distance=100).score()
        assert s_near > s_far

    def test_score_penalized_by_failed_attempts(self):
        s_clean = LocationScore(_loc("A"), distance=10).score()
        s_failed = LocationScore(_loc("B"), distance=10, failed_attempts=3).score()
        assert s_clean > s_failed

    def test_success_rate_boosts_score(self):
        s_lo = LocationScore(_loc("A"), distance=10, success_rate=0.2).score()
        s_hi = LocationScore(_loc("B"), distance=10, success_rate=1.0).score()
        assert s_hi > s_lo

    def test_recent_visit_has_low_freshness_bonus(self):
        now = time.time()
        s_stale = LocationScore(_loc("A"), distance=10, last_visit=0).score()
        s_fresh = LocationScore(_loc("B"), distance=10, last_visit=now - 1).score()
        # Stale (never visited) gets full 10-point bonus; fresh (just visited) ~0
        assert s_stale > s_fresh


class TestLocationStats:
    def test_record_visit_updates_counts(self):
        stats = LocationStats()
        stats.record_visit("Minoc Mine", success=True)
        stats.record_visit("Minoc Mine", success=True)
        stats.record_visit("Minoc Mine", success=False)
        score = stats.get_score(_loc("Minoc Mine"), distance=20)
        assert abs(score.success_rate - (2 / 3)) < 0.01

    def test_record_failure_accumulates(self):
        stats = LocationStats()
        stats.record_failure("Bad Vendor")
        stats.record_failure("Bad Vendor")
        score = stats.get_score(_loc("Bad Vendor"), distance=10)
        assert score.failed_attempts == 2

    def test_failed_attempts_decay_after_5min(self):
        stats = LocationStats()
        stats.record_failure("Bad Vendor")
        # Simulate 6-minute-old failure
        stats._failed_at["Bad Vendor"] = time.time() - 360
        score = stats.get_score(_loc("Bad Vendor"), distance=10)
        assert score.failed_attempts == 0  # decayed

    def test_decay_resets_counter_so_later_failure_starts_fresh(self):
        """After the decay window, the persistent counter must reset.

        Regression guard: a location that failed many times, waited out the
        5-min cooldown, then fails once more should be penalised as a SINGLE
        fresh failure — not as (old_count + 1), which would permanently
        blacklist any location that ever failed repeatedly.
        """
        stats = LocationStats()
        for _ in range(5):
            stats.record_failure("Flaky Mine")
        # Age the failure past the 5-minute decay window.
        stats._failed_at["Flaky Mine"] = time.time() - 360
        # Reading the score during the decay window must clear the counter.
        decayed = stats.get_score(_loc("Flaky Mine"), distance=10)
        assert decayed.failed_attempts == 0
        # A single new failure now starts the count over at 1, not 6.
        stats.record_failure("Flaky Mine")
        after = stats.get_score(_loc("Flaky Mine"), distance=10)
        assert after.failed_attempts == 1

    def test_unvisited_location_gets_default_score(self):
        stats = LocationStats()
        score = stats.get_score(_loc("New Place"), distance=10)
        assert score.success_rate == 1.0
        assert score.failed_attempts == 0
        assert score.last_visit == 0.0


class TestCostWeightedSelection:
    def test_prefers_closer_when_all_else_equal(self):
        """With no history, closer location wins."""
        stats = LocationStats()
        near = stats.get_score(_loc("A"), distance=10).score()
        far = stats.get_score(_loc("B"), distance=50).score()
        assert near > far

    def test_prefers_fresh_over_recently_failed(self):
        """A distant-but-clean location beats a close-but-repeatedly-failed one."""
        stats = LocationStats()
        # Bad local vendor: 3 failures
        stats.record_failure("BadLocal")
        stats.record_failure("BadLocal")
        stats.record_failure("BadLocal")
        close_bad = stats.get_score(_loc("BadLocal"), distance=10).score()
        # Fresh distant vendor
        distant_good = stats.get_score(_loc("FreshFar"), distance=100).score()
        # 3 failures → 150 penalty, distance gap 90 → distant_good wins
        assert distant_good > close_bad
