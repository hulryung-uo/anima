"""Mining expedition session state.

A single `MiningExpedition` instance lives on the Planner and is
published to the blackboard each tick. It owns the session phase,
the list of ground-ore pile memories, and the predicates that
decide when to change phase.

Phases:
- IDLE:          initial state; first successful mine promotes to MINING
- MINING:        pick banks, drop ore on ground at each spot
- COLLECTING:    tour remembered piles, pick up, smelt at forge, repeat
- CRAFTING_TRIP: craft ingots → weapons, sell, return to the mine area

See docs/superpowers/specs/2026-04-14-batch-mining-design.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

import structlog

logger = structlog.get_logger()

# Batch thresholds — mine multiple spots before smelting, accumulate ingots
# before crafting. Moved from planner.py (same values).
BATCH_SMELT_ORE = 8        # accumulate this many ore before a forge run
BATCH_CRAFT_INGOTS = 16    # accumulate ingots before crafting (≈2 weapons)


class Phase(str, Enum):
    IDLE          = "idle"
    MINING        = "mining"
    COLLECTING    = "collecting"
    CRAFTING_TRIP = "crafting_trip"


@dataclass
class PileRecord:
    x: int
    y: int
    bank_key: tuple[int, int]
    est_amount: int
    last_seen_ts: float


@dataclass
class MiningExpedition:
    phase: Phase = Phase.IDLE
    home_base: tuple[int, int] | None = None
    piles: list[PileRecord] = field(default_factory=list)
    phase_started_at: float = 0.0
    cycles_completed: int = 0

    # --- Mutators ---

    def note_ore_mined(self, x: int, y: int, bank_key: tuple[int, int]) -> None:
        """Register a successful mine at (x, y).

        Increments the existing pile at that position, or creates a new
        one. Transitions IDLE → MINING.

        `home_base` is set to (x, y) on the *first* call only and is
        never updated thereafter — it anchors the expedition to the
        spot where mining began so the agent can return there after
        the forge/sell trip.
        """
        now = time.time()
        if self.home_base is None:
            self.home_base = (x, y)
        if self.phase == Phase.IDLE:
            self.transition_to(Phase.MINING)
        for pile in self.piles:
            if pile.x == x and pile.y == y:
                pile.est_amount += 1
                pile.last_seen_ts = now
                return
        self.piles.append(PileRecord(
            x=x, y=y, bank_key=bank_key, est_amount=1, last_seen_ts=now,
        ))

    def mark_pile_collected(self, pile: PileRecord) -> None:
        """Remove the pile from memory. No-op if not present."""
        try:
            self.piles.remove(pile)
        except ValueError:
            pass

    def prune_stale_piles(self, decay_s: float = 1200.0) -> None:
        """Drop piles older than decay_s seconds.

        Boundary is inclusive: a pile whose `last_seen_ts` is exactly
        `decay_s` seconds old (i.e. equal to the cutoff) is *kept*.
        Only piles strictly older than the cutoff are removed.
        """
        cutoff = time.time() - decay_s
        self.piles = [p for p in self.piles if p.last_seen_ts >= cutoff]

    def transition_to(self, new_phase: Phase) -> None:
        """Change phase and log the transition."""
        if new_phase == self.phase:
            return
        old = self.phase
        self.phase = new_phase
        self.phase_started_at = time.time()
        logger.info(
            "expedition_phase",
            from_=old.value,
            to=new_phase.value,
            piles=len(self.piles),
            cycles=self.cycles_completed,
        )
