"""A confirmed melee kill must never reward as a (negative) disengage.

The HP-lost penalty tapers the kill bonus but must preserve its sign: this
reward feeds the live location-value map and per-context action-stats, so an
inverted kill reward would train the LLM to *avoid* successful combat.
"""

from __future__ import annotations

from anima.skills.combat.melee import (
    DISENGAGE_REWARD,
    KILL_REWARD,
    KILL_REWARD_FLOOR,
    _combat_reward,
)


def test_clean_kill_keeps_full_bonus() -> None:
    # No HP lost -> the undiminished kill reward.
    assert _combat_reward(target_killed=True, hp_lost=0) == KILL_REWARD


def test_hard_won_kill_stays_strictly_positive() -> None:
    # 60 HP lost would naively be 15 - 60*0.3 = -3.0 (a negative reward for a
    # WIN). The clamp must keep it strictly positive so the kill reinforces.
    reward = _combat_reward(target_killed=True, hp_lost=60)
    assert reward > 0.0
    assert reward >= KILL_REWARD_FLOOR


def test_pyrrhic_kill_never_drops_below_floor() -> None:
    # Even an absurd HP cost cannot push a confirmed kill to/below zero.
    assert _combat_reward(target_killed=True, hp_lost=10_000) == KILL_REWARD_FLOOR


def test_kill_reward_decreases_with_damage_taken() -> None:
    # The penalty still tapers magnitude (more HP lost -> smaller reward),
    # it just can't invert the sign.
    cheap = _combat_reward(target_killed=True, hp_lost=10)
    costly = _combat_reward(target_killed=True, hp_lost=40)
    assert cheap > costly > 0.0


def test_disengage_is_always_negative() -> None:
    # A disengage is negative reinforcement at every HP cost, including zero.
    assert _combat_reward(target_killed=False, hp_lost=0) == DISENGAGE_REWARD
    assert _combat_reward(target_killed=False, hp_lost=30) < DISENGAGE_REWARD


def test_kill_always_beats_disengage_for_same_fight() -> None:
    # For any given HP cost, killing must out-reward giving up — otherwise the
    # learned signal cannot distinguish success from failure.
    for hp_lost in (0, 15, 50, 100, 500):
        assert _combat_reward(True, hp_lost) > _combat_reward(False, hp_lost)
