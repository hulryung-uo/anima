"""Reward signals for lightweight RL learning."""

from __future__ import annotations

# Reward values for different outcomes
REWARDS: dict[str, float] = {
    "goal_arrived": 10.0,
    "goal_failed": -5.0,
    "speech_responded": 3.0,
    "speech_ignored": -1.0,
    "walk_denied": -2.0,
    "new_place_visited": 5.0,
    "player_greeted": 2.0,
    "damage_taken": -10.0,
    "item_acquired": 5.0,
}


def get_reward(signal: str) -> float:
    """Look up a reward value by signal name."""
    return REWARDS.get(signal, 0.0)
