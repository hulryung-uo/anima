# Batch Mining Expedition — Design

**Status:** Proposed
**Date:** 2026-04-14
**Author:** Claude / Daekeun Kang

## Problem

The agent currently completes one mine→smelt→craft→sell cycle per ~100 minutes and earns roughly 41gp per cycle. The planner's batch thresholds (`BATCH_SMELT_ORE=8`, `BATCH_CRAFT_INGOTS=16`) exist, but Priority 3b triggers `_PickUpAndSmelt` as soon as two or more ore sit on the ground, which defeats accumulation. The result is a tight mine→pickup→smelt ping-pong with no real batching, frequent `planner_health_loop_detected` warnings, and slow progress on gold and skill gain.

The desired behaviour, stated by the user, is:

1. Mine one ServUO 8×8 ore bank until it depletes; drop each mined pile on the ground where it was mined.
2. Move to the next bank, mine it out, leave another pile. Repeat while un-depleted banks exist within scan radius.
3. When no un-depleted banks remain in range, start a collection tour: pick up piles into the backpack up to carry limit, walk to the forge, smelt, return for the remaining piles. Repeat until all piles are collected and smelted.
4. Craft weapons/armor from the smelted ingots when one of these conditions holds after a collection tour:
   - Carrying ingots and weight ≥ 85% of maximum
   - No un-depleted banks available in the current mining area
   - No pickaxe/shovel available
5. Return to the mine after crafting and selling.

## Goals

- At least two full expedition cycles per hour (vs. the current ~0.6 per hour).
- `planner_health_loop_detected` warnings regain signal: appear only when phase-level progress stalls, not during normal operation.
- Each phase transition is observable in structured logs and in `data/state.json` activity.
- No regression in existing planner priorities: survival (HP) and overweight safety-valve smelting continue to preempt all expedition logic.

## Non-Goals

- Persisting pile positions across agent restarts (YAGNI; fallback path handles reboot).
- Rotating across multiple mining regions (Minoc → Cove → …). Home base is fixed to wherever the agent first enters MINING.
- LLM-driven phase selection. The existing `StrategySelector` (5-minute cadence) continues to pick high-level strategies; expedition is a concrete execution layer inside `grind_mining`.
- Pre-validating pile positions (LOS / path). Invalid piles are removed on the first `go_to` failure.

## Approach — Phase-based Session State in a Dedicated Class

The planner keeps its existing priority-rule structure. A new `MiningExpedition` object lives on the planner and is published to the blackboard each tick. It owns the session `phase`, the list of ground-ore pile memories, and the logic that decides when to change phase. Existing priority rules in the planner become phase-aware: Priority 3/3b/5 branches consult the expedition state instead of only inventory counts.

This preserves survival/overweight preemption, keeps phase transition logic testable in isolation, and gives the loop detector a meaningful signal (phase changes introduce procedure diversity).

## Components

### New: `anima/planner/expedition.py`

```python
from dataclasses import dataclass, field
from enum import Enum
import time

class Phase(str, Enum):
    MINING        = "mining"
    COLLECTING    = "collecting"
    CRAFTING_TRIP = "crafting_trip"
    IDLE          = "idle"

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

    def note_ore_mined(self, x, y, bank_key) -> None: ...
    def mark_pile_collected(self, pile: PileRecord) -> None: ...
    def prune_stale_piles(self, decay_s: float = 1200.0) -> None: ...
    def transition_to(self, new_phase: Phase) -> None: ...
    def should_start_collecting(self, ctx) -> bool: ...
    def should_leave_mine(self, ctx) -> bool: ...
    def should_return_to_mine(self, ctx) -> bool: ...
```

The `should_*` predicates encode the phase-exit conditions described below. They take a `BrainContext` (already used elsewhere) so they can read inventory, weight, and map scan results without the planner pre-computing everything.

### Modified: `anima/planner/planner.py`

- `__init__` creates `self._expedition = MiningExpedition()`.
- Each tick's first step publishes the expedition to the blackboard (`ctx.blackboard["expedition"] = self._expedition`) so that `mine.py` and `helpers.py` can read/update it without circular imports.
- `BATCH_SMELT_ORE` and `BATCH_CRAFT_INGOTS` move to `expedition.py` (same values: 8, 16).
- Priority 3 (batch smelt) and Priority 3b (ground-ore pickup) are replaced by a single phase-aware block: when `phase == COLLECTING`, select `_PickUpAndSmelt` for the nearest remembered pile or `smelt_ore` if the backpack already holds ≥2 ore. When `phase == MINING` or `IDLE`, these branches are skipped so accumulation is not interrupted by the two-ore threshold.
- Priority 5 (batch craft) runs only when `phase == CRAFTING_TRIP`.
- Priority 1 (HP) and Priority 2 (overweight safety-valve smelt at ≥2 ore) are unchanged and preempt every phase.
- Priority 4 (no mining tools), Priority 5b (has crafted items to sell), and Priority 7 (fall-through mine) are unchanged. In particular, Priority 7 is what picks `mine_ore` when `phase` is IDLE; the first successful mine then promotes the phase to MINING via `note_ore_mined`.
- When scan detects no mineable banks within `MOVE_RADIUS` and `piles` is non-empty, the planner calls `expedition.transition_to(COLLECTING)` before selecting a procedure this tick.

### Modified: `anima/skills/gathering/mine.py`

- On a successful mine (after the existing ore-drop), call `expedition.note_ore_mined(ss.x, ss.y, bank_key)` if the blackboard has an expedition. This adds or increments a `PileRecord` at the mine location.
- `note_ore_mined` sets `home_base` to the current position if `None`, and transitions `phase` from IDLE to MINING if currently IDLE. Combined, these make the first successful mine the implicit "expedition start" signal.
- No other changes to the mining skill.

### Modified: `anima/planner/helpers.py` — `_PickUpAndSmelt`

- The procedure currently accepts a list of ground ore items found by `_find_ground_ore`. It will be updated so that, when an expedition is present with non-empty `piles`, it prefers the nearest pile position: `go_to(pile.x, pile.y)` then pick up all ore within the 2-tile pickup range.
- After a successful pick-up, call `expedition.mark_pile_collected(pile)`.
- If `go_to` fails or no ore is found at the pile location, remove the pile from memory (log at `info`).
- If `expedition` is absent or `piles` is empty, fall back to the existing "ground ore found in perception" behavior. This keeps the fallback path for agent restart and for operators running without the expedition wiring.

### Unchanged

- `CircuitBreaker` for depleted banks and pickup failures.
- `craft_blacksmith`, `sell_to_vendor`, `buy_from_vendor`, `make_tools` procedures.
- `RoamingHelper`, `DeadlockResolver`.
- `StrategySelector` and its exclusion lists.

## Phase Transitions

### MINING → COLLECTING
- Trigger: `_find_mineable_tile(ctx)` returns `None` for both the immediate mining range and the expanded `MOVE_RADIUS=24` scan, **and** `len(piles) >= 1`.
- Side effect: `transition_to(COLLECTING)` is called by the planner when it detects the above on entry to the MINING branch.
- If `piles` is empty when scan fails, the planner falls through to `RoamingHelper.move_to_location("mine", "mining")` so the agent can seek another mine area. This does not count as a phase transition; the expedition stays in MINING.

### COLLECTING (internal loop)
- Planner selects `_PickUpAndSmelt` for the pile closest to the current position.
- When backpack weight reaches the `weight_max - 50` buffer during pickup, `_PickUpAndSmelt` stops adding ore and proceeds to the forge to smelt.
- After smelting returns control to the planner, the phase stays COLLECTING and the next pile is selected.

### COLLECTING → CRAFTING_TRIP
- Trigger: `piles` is empty (all collected) **and** at least one of:
  - Inventory ingots ≥ `BATCH_CRAFT_INGOTS` (16), or
  - Weight ≥ 85% of maximum and inventory contains ingots, or
  - The agent no longer has a pickaxe/shovel.
- Side effect: `transition_to(CRAFTING_TRIP)`. The planner then runs existing Priority 5 branches (craft, sell, buy tongs/tools) to complete the trip.

### COLLECTING → MINING
- Trigger: `piles` is empty and none of the CRAFTING_TRIP conditions hold.
- Side effect: `transition_to(MINING)`. The planner resumes mining; `_find_mineable_tile` scan may now find a respawned bank.

### CRAFTING_TRIP → MINING
- Trigger: ingots < 4 and no crafted items remain in the backpack (i.e., sold), and the agent's current position is within 30 tiles of `home_base`.
- Side effect: `transition_to(MINING)`, `cycles_completed += 1`.
- If the agent is not yet near `home_base`, the planner continues with the existing `move_to_location` helper toward the mining area; no phase change yet.

### IDLE → MINING (implicit)
- Trigger: first successful `mine_ore` call returns to `note_ore_mined`, which flips phase to MINING if currently IDLE.
- The planner does not need a dedicated transition here; the flip happens inside `note_ore_mined`.

### Any → IDLE (watchdog)
- Trigger: `phase_started_at` older than 10 minutes with no successful procedure in the current phase.
- Side effect: log `warning`, `transition_to(IDLE)`, clear `piles`. The next tick runs the existing priority rules without phase-specific gates; Priority 7 (fall-through mine) or Priority 4 (no tools) will fire and the cycle begins again.

### Interrupts (phase-agnostic)
- Priority 1 (HP ≤ 30% → `heal_self`) runs before any phase logic.
- Priority 2 (weight > 85% with ≥2 smeltable ore → immediate `smelt_ore`) runs before phase logic; it does **not** change `phase`.

## Data Flow

```
mine_ore.execute()
    → server returns ore → item appears in backpack
    → mine.py drops it at player position (existing)
    → mine.py calls expedition.note_ore_mined(x, y, bank_key)

planner tick
    → publish expedition to blackboard
    → run survival & overweight priorities
    → consult expedition.phase
        MINING    → select mine_ore or move_to_mineable_tile
        COLLECT   → select _PickUpAndSmelt(nearest pile) or smelt_ore
        CRAFT     → select craft_blacksmith / sell_to_vendor / buy
        IDLE      → let planner re-enter flow from scratch

_PickUpAndSmelt.run()
    → go_to(pile.x, pile.y) → pickup ore within 2 tiles
    → drop into backpack → walk to forge → smelt
    → expedition.mark_pile_collected(pile)

State changes land in:
    data/state.json (existing snapshot writer)
    data/events.jsonl via logger.info("expedition_phase", ...)
    Activity log entries tagged "expedition_cycle_complete" on cycle rollover
```

## Failure Handling

### Mining
- False-positive depleted bank: existing `CircuitBreaker` re-opens the bank after 10 minutes; the next scan finds it.
- Monster attack during mining: Priority 1 preempts; phase unchanged.
- Pickaxe breaks: detected in the MINING branch via existing `has_mining_tool` check. If `piles` is empty, transition directly to CRAFTING_TRIP; otherwise, transition to COLLECTING first.

### Collection
- Pile gone (server decay or other player): `_PickUpAndSmelt` finds no ore at the expected position, removes the pile, logs `info`.
- LOS / z pickup refusal: existing `_ore_pickup_breaker` marks the ore serial as junk after two failures.
- Forge unreachable: existing `_move_fail_until` cooldown; pile remains in memory and is retried on the next tick. If COLLECTING exceeds 5 minutes without any pile being marked collected, the watchdog fires (see IDLE).

### Crafting
- Insufficient metal: existing `_craft_material_breaker` (300s cooldown). When cooldown active, planner falls through to raw-ingot sell.
- Missing tongs: existing Priority 5 branches (buy tongs, or sell a few ingots to fund the purchase).
- Vendor unreachable: existing roaming fallback.

### Expedition Watchdog
- `phase_started_at` older than 10 minutes + no successful procedure: `transition_to(IDLE)`, clear piles, log warning. The next tick re-enters from scratch.
- Stale piles (last_seen_ts > 20 minutes): `prune_stale_piles` drops them.

### Concurrency
- The blackboard is single-writer from the planner task. No locks needed.
- Tests inject a `MiningExpedition` instance directly via an optional `__init__` argument.

## Observability

- `logger.info("expedition_phase", from_=old, to=new, piles=n, cycles=c)` on every transition.
- `logger.info("expedition_cycle_complete", cycles=c, duration_s=n)` on CRAFTING_TRIP → MINING.
- `logger.warning("expedition_watchdog", phase=p, stuck_s=n)` on watchdog fires.
- Activity log entry `expedition_cycle_complete` appended so the supervisor sees progress.

## Testing

- `tests/planner/test_expedition_phase_transitions.py`
  - MINING → COLLECTING when scan empty and piles non-empty
  - MINING → MINING (no transition) when scan empty and piles empty
  - COLLECTING → MINING when piles empty and no craft condition holds
  - COLLECTING → CRAFTING_TRIP when piles empty and ingots ≥ 16
  - COLLECTING → CRAFTING_TRIP when weight ≥ 85% with ingots
  - COLLECTING → CRAFTING_TRIP when pickaxe missing
  - CRAFTING_TRIP → MINING when ingots < 4 and near home_base
  - Watchdog fires after 10 minutes of no progress
- `tests/planner/test_expedition_pile_tracking.py`
  - `note_ore_mined` adds a new pile on first call, increments on repeat
  - `mark_pile_collected` removes the pile
  - `prune_stale_piles` drops entries older than 20 minutes
  - `home_base` is set on first mine and not overwritten
- Existing planner integration tests are extended to cover:
  - Priority 3/3b path now consults `phase` and skips when phase is MINING
  - Fallback path (`expedition` absent from blackboard) still works

## Rollout

1. Land `expedition.py` with full unit test coverage (no planner/mine wiring yet).
2. Add the `note_ore_mined` hook to `mine.py`, gated on blackboard presence.
3. Switch `planner.py` Priority 3/3b/5 to phase-aware. Keep the fallback ground-ore path intact.
4. Update `_PickUpAndSmelt` to prefer remembered piles. Keep the perception-based fallback.
5. Run the full planner test suite. Observe one live cycle end-to-end. If the cycle does not complete within two hours or `expedition_cycle_complete` never fires, revert.

No feature flag. The change is small enough and the revert path clean enough that a flag adds noise.

## Completion Criteria

- A full MINING → COLLECTING → (MINING → COLLECTING)* → CRAFTING_TRIP → MINING cycle is visible in logs via `expedition_phase` entries.
- `cycles_completed` increments at least twice per hour on the Minoc mining loop.
- `planner_health_loop_detected` warnings drop to under one per 10 minutes in normal operation.
- No existing planner test regressions.
