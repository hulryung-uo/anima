"""Tests for MiningExpedition state machine."""
from __future__ import annotations

import time

from anima.planner.expedition import (
    BATCH_CRAFT_INGOTS,
    BATCH_SMELT_ORE,
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

    def test_transition_to_updates_phase_started_at(self):
        exp = MiningExpedition()
        t0 = time.time()
        exp.transition_to(Phase.MINING)
        assert exp.phase == Phase.MINING
        assert exp.phase_started_at >= t0

    def test_prune_stale_piles_drops_old_entries(self):
        exp = MiningExpedition()
        now = time.time()
        exp.piles = [
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=now - 1800),  # 30 min old
            PileRecord(x=2, y=2, bank_key=(0, 0), est_amount=1, last_seen_ts=now - 600),   # 10 min old
        ]
        exp.prune_stale_piles(decay_s=1200.0)  # 20 min
        assert len(exp.piles) == 1
        assert exp.piles[0].x == 2

    def test_batch_constants_moved(self):
        """Sanity: the batch thresholds live on the expedition module."""
        assert BATCH_SMELT_ORE == 8
        assert BATCH_CRAFT_INGOTS == 16
