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

# Auxiliary trigger for MINING → COLLECTING. The primary trigger is "no
# mineable banks in range", but in dense mining areas (e.g. Minoc) banks
# respawn on a 10 min cooldown and the scan almost never reports empty,
# leaving MINING to only exit via the watchdog — which clears piles and
# resets progress.
#
# This threshold is the total ORE accumulated across all piles (sum of
# est_amount), not the number of distinct pile locations — an agent that
# mines repeatedly at the same (x,y) still produces one pile with a
# growing est_amount, and counting locations would never trigger.
AUX_COLLECT_ORE_THRESHOLD = 10


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
    # Timestamp of the last *verified* progress event (a successful mine).
    # The watchdog measures staleness from this, not from phase entry, so a
    # phase that is making real progress (e.g. productively mining in a dense
    # area where the bank scan never reports empty) is never treated as
    # "stuck". 0.0 means "no progress yet" — the watchdog then falls back to
    # phase_started_at, preserving the original phase-age behaviour.
    last_progress_at: float = 0.0

    # --- Mutators ---

    def note_ore_mined(self, x: int, y: int, bank_key: tuple[int, int],
                       ground_pile: bool = True) -> None:
        """Register a successful mine at (x, y).

        Increments the existing pile at that position, or creates a new
        one. Transitions IDLE → MINING.

        `ground_pile=False` records the mine for the expedition state
        machine (phase, home_base) WITHOUT registering a pile — used when
        the ore stayed in the backpack because the server rejected the
        ground drop (AOS shards bounce drops onto the dropper's own tile).
        Registering unverified drops creates phantom piles the collect leg
        can never find ("pile empty at target").

        `home_base` is set to (x, y) on the *first* call only and is
        never updated thereafter — it anchors the expedition to the
        spot where mining began so the agent can return there after
        the forge/sell trip.
        """
        now = time.time()
        # A successful mine is real progress — re-arm the watchdog so an
        # actively-mining phase is never wiped for being "stuck".
        self.last_progress_at = now
        if self.home_base is None:
            self.home_base = (x, y)
        if self.phase == Phase.IDLE:
            self.transition_to(Phase.MINING)
        if not ground_pile:
            return
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
        # New phase starts with a clean slate; staleness is measured from
        # entry until the first in-phase progress event re-arms it.
        self.last_progress_at = 0.0
        logger.info(
            "expedition_phase",
            from_=old.value,
            to=new_phase.value,
            piles=len(self.piles),
            cycles=self.cycles_completed,
        )

    # --- Phase-transition predicates ---

    def should_start_collecting(self, scan_empty: bool) -> bool:
        """MINING → COLLECTING.

        Primary trigger: no mineable banks in range and at least one pile.
        Aux trigger: enough total ore accumulated to be worth a tour, even
        if banks are still available — prevents MINING from running forever
        in dense mining areas where scan never reports empty.
        """
        if self.phase != Phase.MINING:
            return False
        if not self.piles:
            return False
        if scan_empty:
            return True
        total_ore = sum(p.est_amount for p in self.piles)
        return total_ore >= AUX_COLLECT_ORE_THRESHOLD

    def should_leave_mine(
        self,
        *,
        ingot_count: int,
        weight_ratio: float,
        has_pickaxe: bool,
    ) -> bool:
        """COLLECTING → CRAFTING_TRIP (assumes piles already empty).

        Called only when collection is complete (`piles == []`). Any of:
        - Enough ingots for a craft batch
        - Overweight AND actually carrying ingots (not rocks)
        - No pickaxe: mining can't continue, dispose of ingots first
        """
        if self.phase != Phase.COLLECTING:
            return False
        if ingot_count >= BATCH_CRAFT_INGOTS:
            return True
        if weight_ratio >= 0.85 and ingot_count > 0:
            return True
        if not has_pickaxe:
            return True
        return False

    def should_return_to_mine(
        self,
        *,
        ingot_count: int,
        crafted_count: int,
        near_home: bool,
    ) -> bool:
        """CRAFTING_TRIP → MINING when ingots disposed and we're back at the mine."""
        if self.phase != Phase.CRAFTING_TRIP:
            return False
        return ingot_count < 4 and crafted_count == 0 and near_home

    def watchdog_expired(self, max_phase_s: float = 600.0) -> bool:
        """True if the current phase has been active too long without progress.

        Returns False in IDLE — the watchdog is meaningless before a phase
        has been entered, and the initial phase_started_at=0.0 would otherwise
        spuriously trip.

        "Without progress" is measured from the most recent verified progress
        event (`last_progress_at`) when one exists, otherwise from phase entry
        (`phase_started_at`). This keeps a productively-mining phase alive even
        when its total age exceeds `max_phase_s` — the watchdog only fires when
        the agent has genuinely made no progress for `max_phase_s`, which is the
        stall it is meant to catch (and avoids wiping real accumulated piles).
        """
        if self.phase == Phase.IDLE:
            return False
        reference = max(self.phase_started_at, self.last_progress_at)
        return time.time() - reference > max_phase_s
