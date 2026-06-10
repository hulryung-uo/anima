"""Behavior descriptor — the QD axes (FOUNDRY.md §4).

Computed purely from a parsed trajectory (behavior, not code/intent). Captures
WHAT KIND of soul the agent is; fitness (§5) captures HOW GOOD at being that
kind. The two are deliberately decoupled — profession_focus reads *which* skill
category (not how much), the temperament axes read action-type *fractions* (not
success) — so the grid expresses real diversity, not a fitness ramp.

4 locked axes: profession_focus (categorical, 7 bins) + sociability / aggression
/ mobility (continuous, 3 bins each). Phase-0 ACTIVE grid is
profession_focus × sociability (21 cells); aggression and mobility are computed
but not yet part of the cell key. Bin boundaries are kernel-owned and
calibrated in Phase 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from foundry.kernel import uoconst
from foundry.kernel.trajectory import TrajectorySummary

# Continuous-axis bin boundaries (low | mid | high). Calibratable.
SOCIABILITY_EDGES = (0.02, 0.10)        # speech / total actions
AGGRESSION_EDGES = (0.02, 0.15)         # attacks / total actions
MOBILITY_EDGES = (10.0, 40.0)           # unique regions / hour

BIN_NAMES = ("low", "mid", "high")

# Phase-0 active grid axes (FOUNDRY.md §4 phased activation).
ACTIVE_AXES: tuple[str, ...] = ("profession_focus", "sociability")


def _bin(value: float, edges: tuple[float, float]) -> int:
    if value < edges[0]:
        return 0
    if value < edges[1]:
        return 1
    return 2


@dataclass
class Descriptor:
    profession_focus: str = uoconst.NONE
    sociability_bin: int = 0
    aggression_bin: int = 0
    mobility_bin: int = 0

    # raw axis values (interpretability)
    sociability: float = 0.0
    aggression: float = 0.0
    mobility_rate: float = 0.0
    profession_gains: dict[str, float] = field(default_factory=dict)

    @property
    def cell(self) -> tuple:
        """Active-grid cell key (Phase 0: profession_focus × sociability)."""
        parts: list = []
        for axis in ACTIVE_AXES:
            if axis == "profession_focus":
                parts.append(self.profession_focus)
            elif axis == "sociability":
                parts.append(self.sociability_bin)
            elif axis == "aggression":
                parts.append(self.aggression_bin)
            elif axis == "mobility":
                parts.append(self.mobility_bin)
        return tuple(parts)

    @property
    def full_cell(self) -> tuple:
        """Full 4-axis cell key (for when all axes are activated)."""
        return (
            self.profession_focus,
            self.sociability_bin,
            self.aggression_bin,
            self.mobility_bin,
        )

    def label(self) -> str:
        soc = BIN_NAMES[self.sociability_bin]
        return f"{self.profession_focus.lower()}/{soc}-social"


def compute_descriptor(summ: TrajectorySummary) -> Descriptor:
    actions = max(1, summ.total_actions)

    gains = summ.profession_skill_gains()
    if gains:
        profession = max(gains, key=gains.get)
    else:
        profession = uoconst.NONE

    sociability = summ.speech_sent / actions
    aggression = summ.attacks_initiated / actions
    mobility_rate = summ.unique_regions / max(summ.duration_h, 1.0 / 60.0)

    return Descriptor(
        profession_focus=profession,
        sociability_bin=_bin(sociability, SOCIABILITY_EDGES),
        aggression_bin=_bin(aggression, AGGRESSION_EDGES),
        mobility_bin=_bin(mobility_rate, MOBILITY_EDGES),
        sociability=sociability,
        aggression=aggression,
        mobility_rate=mobility_rate,
        profession_gains=gains,
    )
