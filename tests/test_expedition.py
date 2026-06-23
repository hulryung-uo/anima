"""Tests for MiningExpedition state machine."""
from __future__ import annotations

import time

from anima.planner.expedition import (
    BATCH_CRAFT_INGOTS,
    BATCH_SMELT_ORE,
    RETURN_INGOT_FLOOR,
    MiningExpedition,
    Phase,
    PileRecord,
)


class TestMiningExpeditionBasics:
    def test_default_state_is_idle(self):
        exp = MiningExpedition()
        assert exp.phase == Phase.IDLE
        assert exp.piles == []
        assert exp.home_base is None
        assert exp.cycles_completed == 0

    def test_note_ore_mined_adds_pile(self):
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        assert len(exp.piles) == 1
        assert exp.piles[0].x == 2460
        assert exp.piles[0].y == 558
        assert exp.piles[0].bank_key == (307, 69)
        assert exp.piles[0].est_amount == 1

    def test_note_ore_mined_increments_same_pile(self):
        """Repeated mining at the same (x, y) increments est_amount, not count."""
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        assert len(exp.piles) == 1
        assert exp.piles[0].est_amount == 2

    def test_note_ore_mined_sets_home_base_first_time(self):
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        assert exp.home_base == (2460, 558)

        # Second mine at a different spot does NOT overwrite home_base
        exp.note_ore_mined(x=2470, y=560, bank_key=(308, 70))
        assert exp.home_base == (2460, 558)

    def test_note_ore_mined_transitions_idle_to_mining(self):
        exp = MiningExpedition()
        assert exp.phase == Phase.IDLE
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        assert exp.phase == Phase.MINING

    def test_note_ore_mined_during_mining_keeps_phase(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        assert exp.phase == Phase.MINING

    def test_mark_pile_collected_removes_it(self):
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        pile = exp.piles[0]
        exp.mark_pile_collected(pile)
        assert exp.piles == []

    def test_mark_pile_collected_missing_is_noop(self):
        """Calling with a pile not in the list doesn't crash."""
        exp = MiningExpedition()
        phantom = PileRecord(x=0, y=0, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        exp.mark_pile_collected(phantom)
        assert exp.piles == []

    def test_mark_pile_collected_rearms_progress_clock(self):
        """Collecting a pile is COLLECTING-phase progress: it must re-arm the
        watchdog so a long-but-productive collection tour is not wiped to IDLE.

        Regression for the watchdog false-positive: `note_ore_mined` (the only
        other progress signal) fires only during MINING, so without this the
        COLLECTING phase has no way to mark progress and a tour that runs past
        max_phase_s gets its remaining piles cleared mid-collect.
        """
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        exp.transition_to(Phase.COLLECTING)  # clears last_progress_at to 0.0
        now = time.time()
        # A collection tour that entered the phase 11m40s ago with no progress
        # recorded would trip the watchdog...
        exp.phase_started_at = now - 700.0
        assert exp.last_progress_at == 0.0
        assert exp.watchdog_expired(max_phase_s=600.0) is True
        # ...but collecting a pile is verified progress and re-arms it.
        pile = exp.piles[0]
        exp.mark_pile_collected(pile)
        assert exp.last_progress_at >= now
        assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_mark_pile_collected_noop_does_not_rearm(self):
        """Removing a pile that isn't tracked is not progress — no re-arm."""
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        exp.last_progress_at = 0.0
        phantom = PileRecord(x=0, y=0, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        exp.mark_pile_collected(phantom)
        assert exp.last_progress_at == 0.0

    def test_transition_to_updates_phase_started_at(self):
        exp = MiningExpedition()
        t0 = time.time()
        exp.transition_to(Phase.MINING)
        assert exp.phase == Phase.MINING
        assert exp.phase_started_at >= t0

    def test_transition_to_same_phase_is_noop(self):
        """Self-transition must NOT update phase_started_at."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        first_ts = exp.phase_started_at
        # Sleep a hair so any incorrect update would be observable
        time.sleep(0.01)
        exp.transition_to(Phase.MINING)
        assert exp.phase == Phase.MINING
        assert exp.phase_started_at == first_ts

    def test_prune_stale_piles_drops_old_entries(self):
        exp = MiningExpedition()
        now = time.time()
        exp.piles = [
            # 30 min old
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=now - 1800),
            # 10 min old
            PileRecord(x=2, y=2, bank_key=(0, 0), est_amount=1, last_seen_ts=now - 600),
        ]
        exp.prune_stale_piles(decay_s=1200.0)  # 20 min
        assert len(exp.piles) == 1
        assert exp.piles[0].x == 2

    def test_batch_constants_moved(self):
        """Sanity: the batch thresholds live on the expedition module."""
        assert BATCH_SMELT_ORE == 8
        assert BATCH_CRAFT_INGOTS == 16


class TestPhaseTransitionPredicates:
    def test_should_start_collecting_true_when_scan_empty_and_piles(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        assert exp.should_start_collecting(scan_empty=True) is True

    def test_should_start_collecting_false_when_piles_empty(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        assert exp.should_start_collecting(scan_empty=True) is False

    def test_should_start_collecting_false_when_scan_nonempty_below_threshold(self):
        """Total ore below AUX_COLLECT_ORE_THRESHOLD → stay MINING."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))  # est=1
        assert exp.should_start_collecting(scan_empty=False) is False

    def test_should_start_collecting_true_by_total_ore_single_pile(self):
        """Repeated mining at one spot hits the ore threshold even with 1 pile."""
        from anima.planner.expedition import AUX_COLLECT_ORE_THRESHOLD

        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        # Stays at one spot → one pile whose est_amount grows each mine.
        for _ in range(AUX_COLLECT_ORE_THRESHOLD):
            exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        assert len(exp.piles) == 1
        assert exp.piles[0].est_amount == AUX_COLLECT_ORE_THRESHOLD
        assert exp.should_start_collecting(scan_empty=False) is True

    def test_should_start_collecting_true_by_total_ore_multi_pile(self):
        """Sum of est_amount across piles crosses threshold → COLLECTING."""
        from anima.planner.expedition import AUX_COLLECT_ORE_THRESHOLD

        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        half = AUX_COLLECT_ORE_THRESHOLD // 2
        # Two spots, each contributing half; sum reaches threshold.
        for _ in range(half):
            exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        for _ in range(AUX_COLLECT_ORE_THRESHOLD - half):
            exp.note_ore_mined(x=200, y=200, bank_key=(25, 25))
        assert len(exp.piles) == 2
        assert sum(p.est_amount for p in exp.piles) == AUX_COLLECT_ORE_THRESHOLD
        assert exp.should_start_collecting(scan_empty=False) is True

    def test_should_start_collecting_false_outside_mining(self):
        exp = MiningExpedition()  # IDLE
        exp.piles.append(
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        )
        assert exp.should_start_collecting(scan_empty=True) is False

    def test_should_leave_mine_by_ingot_count(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=BATCH_CRAFT_INGOTS, weight_ratio=0.5, has_pickaxe=True,
        ) is True

    def test_should_leave_mine_by_weight_with_ingots(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=4, weight_ratio=0.90, has_pickaxe=True,
        ) is True

    def test_should_leave_mine_weight_without_ingots_is_false(self):
        """Overweight from non-ingots (e.g., rocks) is not a craft trigger."""
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=0, weight_ratio=0.95, has_pickaxe=True,
        ) is False

    def test_should_leave_mine_by_missing_pickaxe(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=0, weight_ratio=0.2, has_pickaxe=False,
        ) is True

    def test_should_leave_mine_false_when_nothing_triggers(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=4, weight_ratio=0.5, has_pickaxe=True,
        ) is False

    def test_should_leave_mine_false_outside_collecting(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        assert exp.should_leave_mine(
            ingot_count=100, weight_ratio=0.99, has_pickaxe=False,
        ) is False

    def test_should_return_to_mine_true_when_done_and_near_home(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        assert exp.should_return_to_mine(
            ingot_count=2, crafted_count=0, near_home=True,
        ) is True

    def test_should_return_to_mine_false_when_still_has_ingots(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        assert exp.should_return_to_mine(
            ingot_count=RETURN_INGOT_FLOOR, crafted_count=0, near_home=True,
        ) is False

    def test_should_return_to_mine_false_when_still_has_crafted(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        assert exp.should_return_to_mine(
            ingot_count=2, crafted_count=1, near_home=True,
        ) is False

    def test_should_return_to_mine_false_when_far_from_home(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        assert exp.should_return_to_mine(
            ingot_count=2, crafted_count=0, near_home=False,
        ) is False

    def test_should_return_to_mine_false_outside_crafting_trip(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        assert exp.should_return_to_mine(
            ingot_count=0, crafted_count=0, near_home=True,
        ) is False

    def test_watchdog_expired_when_phase_too_old(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.phase_started_at = time.time() - 700  # 11m40s ago
        assert exp.watchdog_expired(max_phase_s=600.0) is True

    def test_watchdog_not_expired_when_recent(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_should_leave_mine_at_weight_threshold_exact(self):
        """weight_ratio == 0.85 is the inclusive threshold."""
        exp = MiningExpedition()
        exp.transition_to(Phase.COLLECTING)
        assert exp.should_leave_mine(
            ingot_count=1, weight_ratio=0.85, has_pickaxe=True,
        ) is True

    def test_should_return_to_mine_ingot_count_at_boundary(self):
        """ingot_count < RETURN_INGOT_FLOOR: floor-1 returns True, floor blocks."""
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        assert exp.should_return_to_mine(
            ingot_count=RETURN_INGOT_FLOOR - 1, crafted_count=0, near_home=True,
        ) is True
        assert exp.should_return_to_mine(
            ingot_count=RETURN_INGOT_FLOOR, crafted_count=0, near_home=True,
        ) is False

    def test_return_floor_tracks_blacksmith_craft_minimum(self):
        """The CRAFTING_TRIP return floor must equal the craft minimum so a
        leftover that can't start a craft can't be stranded either."""
        from anima.procedures.craft_blacksmith import MIN_INGOTS
        assert RETURN_INGOT_FLOOR == MIN_INGOTS

    def test_leftover_ingots_below_craft_batch_do_not_strand(self):
        """Regression: a craft session ending with 4–7 leftover iron ingots is
        below MIN_INGOTS (can't craft) but was >= the old magic floor of 4
        (couldn't return) — a dead band that wedged CRAFTING_TRIP until the
        watchdog wiped the session. Such remainders must now return to MINING.
        """
        from anima.procedures.craft_blacksmith import MIN_INGOTS
        exp = MiningExpedition()
        exp.transition_to(Phase.CRAFTING_TRIP)
        for leftover in range(4, MIN_INGOTS):  # the previously-stranding band
            assert exp.should_return_to_mine(
                ingot_count=leftover, crafted_count=0, near_home=True,
            ) is True, f"{leftover} leftover ingots must not strand CRAFTING_TRIP"

    def test_watchdog_not_expired_at_exact_limit(self):
        """Equality (phase_started_at == now - max_phase_s) does NOT expire."""
        from unittest.mock import patch

        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        fixed_now = 1_000_000.0
        exp.phase_started_at = fixed_now - 600.0
        with patch("anima.planner.expedition.time.time", return_value=fixed_now):
            assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_watchdog_false_in_idle(self):
        """Fresh expedition in IDLE must never report watchdog expired."""
        exp = MiningExpedition()  # phase=IDLE, phase_started_at=0.0
        assert exp.watchdog_expired(max_phase_s=600.0) is False


class TestWatchdogTracksProgress:
    """The watchdog must measure staleness from the last verified progress
    event, not raw phase age — otherwise a productively-mining phase in a
    dense area (bank scan never empty) gets its accumulated piles wiped
    every `max_phase_s` despite making real progress.
    """

    def test_productive_mining_does_not_trip_watchdog(self):
        """Phase older than max_phase_s but with recent progress is alive."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        now = time.time()
        # Entered the phase 20 minutes ago...
        exp.phase_started_at = now - 1200.0
        # ...but mined successfully 1 minute ago.
        exp.last_progress_at = now - 60.0
        assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_stalled_mining_still_trips_watchdog(self):
        """No progress for longer than max_phase_s → watchdog fires."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        now = time.time()
        exp.phase_started_at = now - 1200.0
        exp.last_progress_at = now - 700.0  # last mine 11m40s ago
        assert exp.watchdog_expired(max_phase_s=600.0) is True

    def test_note_ore_mined_rearms_progress(self):
        """A successful mine updates last_progress_at to ~now."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        # Simulate a long-running phase with no progress recorded yet.
        now = time.time()
        exp.phase_started_at = now - 1200.0
        assert exp.watchdog_expired(max_phase_s=600.0) is True  # stale, no progress
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        assert exp.last_progress_at >= now
        assert exp.watchdog_expired(max_phase_s=600.0) is False  # re-armed

    def test_note_ore_mined_rearms_even_without_ground_pile(self):
        """A verified mine that bounced into the backpack is still progress."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        now = time.time()
        exp.phase_started_at = now - 1200.0
        exp.last_progress_at = 0.0
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12), ground_pile=False)
        # No pile recorded, but progress is still tracked.
        assert exp.piles == []
        assert exp.last_progress_at >= now
        assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_transition_resets_progress_clock(self):
        """Entering a new phase clears stale progress; staleness restarts."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        assert exp.last_progress_at > 0.0
        exp.transition_to(Phase.COLLECTING)
        assert exp.last_progress_at == 0.0
        # Fresh phase: watchdog references phase_started_at (just now) → alive.
        assert exp.watchdog_expired(max_phase_s=600.0) is False

    def test_no_progress_falls_back_to_phase_age(self):
        """Backward compat: with no progress event, behaviour is phase-age."""
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.last_progress_at = 0.0
        exp.phase_started_at = time.time() - 700.0
        assert exp.watchdog_expired(max_phase_s=600.0) is True

    def test_first_mine_from_idle_arms_progress(self):
        """The IDLE -> MINING first mine must record progress, not lose it.

        Regression: `note_ore_mined` stamped `last_progress_at = now` BEFORE
        running the IDLE -> MINING `transition_to`, which resets
        `last_progress_at` to 0.0 — so the very mine that started the phase
        left the progress clock at 0.0. The watchdog then fell back to
        `phase_started_at` and, if the next mine was slow, could declare a
        freshly-productive MINING phase "stuck".
        """
        exp = MiningExpedition()
        assert exp.phase == Phase.IDLE
        before = time.time()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        # Transitioned into MINING and recorded REAL progress (not 0.0).
        assert exp.phase == Phase.MINING
        assert exp.last_progress_at >= before

    def test_slow_second_mine_does_not_trip_watchdog_after_first_mine(self):
        """End-to-end: a single mine from IDLE keeps the phase alive even as
        the phase ages past max_phase_s while waiting on the next mine.

        With the bug, `last_progress_at` was 0.0 after the first mine, so the
        watchdog measured only from `phase_started_at`; backdating that past
        the limit wrongly fired the watchdog despite verified progress.
        """
        exp = MiningExpedition()
        exp.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
        # Simulate the phase having been entered a long time ago, but the
        # last verified mine (progress) only a moment ago.
        exp.phase_started_at = time.time() - 700.0
        # last_progress_at was correctly armed by the mine -> phase is alive.
        assert exp.watchdog_expired(max_phase_s=600.0) is False


class TestWatchdogResetClearsAnchor:
    def test_reset_session_clears_home_base_and_piles(self):
        exp = MiningExpedition()
        exp.note_ore_mined(2460, 558, bank_key=(76, 17))
        assert exp.home_base == (2460, 558)
        assert exp.piles

        exp.reset_session()
        assert exp.home_base is None
        assert exp.piles == []
        assert exp.phase == Phase.IDLE

    def test_reset_session_preserves_lifetime_cycle_counter(self):
        exp = MiningExpedition()
        exp.cycles_completed = 3
        exp.note_ore_mined(100, 100, bank_key=(3, 3))
        exp.reset_session()
        assert exp.cycles_completed == 3

    def test_next_mine_reanchors_after_reset(self):
        """A wiped session must re-anchor at the NEW mining location, not stay
        pinned to the abandoned one. Before the fix the watchdog cleared only
        `piles`, leaving `home_base` set; `note_ore_mined` then refused to
        overwrite it (sets only when None), so a relocated agent ran
        CRAFTING_TRIP against a far-away anchor it could never reach."""
        exp = MiningExpedition()
        exp.note_ore_mined(2460, 558, bank_key=(76, 17))
        assert exp.home_base == (2460, 558)

        exp.reset_session()

        exp.note_ore_mined(5000, 1200, bank_key=(156, 37))
        assert exp.home_base == (5000, 1200)  # re-anchored at B, not A
        assert exp.phase == Phase.MINING
