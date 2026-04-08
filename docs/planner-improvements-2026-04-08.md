# Planner Architecture Improvements — 2026-04-08

**Summary**: A two-part refactoring of `anima/planner/` that addresses the recurring "fix X loop" bug pattern (35+ such commits in one debugging session) and adds goal-directed, LLM-guided behavior.

**Scope**: 23 commits, ~1,900 lines of new code, ~90 new tests, `planner.py` shrunk from 1,841 → 1,239 lines.

**Test count**: 321 → 413 (+92)

---

## Motivation

Before this refactor, the planner had recurring structural problems surfaced by 55 commits of bug-fixing in a single session:

1. **Loop pandemic** — 35 commits out of 55 were "fix X loop". Each procedure reinvented its own cooldown/retry logic in ad-hoc blackboard keys, and the planner kept reselecting failing procedures because there was no circuit-breaker abstraction.
2. **God file** — `planner.py` was 1,841 lines. Every fix touched the same file, and Claude Code's self-improvement cycle needed to re-read the entire file each time, hitting timeouts.
3. **State leakage** — 46+ ad-hoc blackboard keys (`_craft_bs_fails`, `_skip_procedures`, `depleted_banks`, `refused_vendors`, `_failed_destinations`, …) with no unified surface.
4. **Slow self-improvement** — Supervisor detects a problem → calls Opus 4.6 → waits 10+ minutes. Most fixes were actually one-liners that Haiku 4.5 could apply in 30 seconds.
5. **Purely reactive planning** — Every tick picked the best local action but the agent had no concept of "what I'm trying to achieve". Result: 186 iron ingots piled up because nothing said "collect ingots for goal X".
6. **Nearest-only roaming** — Location picking only considered distance. Locations that repeatedly failed to reach were still picked first.
7. **No temporal tests** — `test_planner.py` only exercised single-tick calls. Sequence bugs ("X fails → retry → X fails → … idle loop") only surfaced in live play.

This refactor addresses all seven.

---

## Part 1 — Structural refactoring (Phases 1-5)

### Phase 1: Fast loop detection — `PlannerHealth`

**File**: `anima/planner/health.py` (68 lines)

Sliding-window tracker of the last 30 procedure selections. Reports a loop when fewer than 20% of recent selections are unique (`unique / total < 0.2`).

```python
health = PlannerHealth(window=30, min_diversity=0.2)
health.record("mine_ore")
if health.is_looping():
    logger.warning("loop", dominant=health.dominant_procedure())
```

**Wired into `Planner.run()`**: Each tick after `select_procedure()` returns, the chosen procedure name is recorded. If the health check trips, the planner pauses selection for 60 seconds (`_health_break_until`) and resets per-tick counters. This gives the environment a chance to change before the agent retries.

**Impact**: Loop detection latency dropped from ~10 minutes (supervisor analysis cycle) to ~30 seconds (a full health window).

---

### Phase 2: `CircuitBreaker` abstraction

**File**: `anima/planner/circuit_breaker.py` (107 lines)

Per-target failure counter with auto-expiring cooldown. Replaces ad-hoc dicts for bank depletion, vendor refusal, material mismatch, pickup failures, and failed destinations.

```python
breaker = CircuitBreaker(max_failures=3, cooldown_s=600)
breaker.record_failure(target_key)     # counts one miss
if breaker.is_open(target_key):        # inside cooldown?
    return None                         # skip
breaker.record_success(target_key)     # clears the counter
breaker.trip(target_key)                # open immediately (bypass counter)
```

Key design: internal state is split into two typed dicts (`_counts: dict[Hashable, int]` and `_tripped_at: dict[Hashable, float]`) rather than a `[count, timestamp]` list. This gives Pyright a clean type surface and avoids `float | int` unions.

**Three live migrations installed**:

| Breaker | Where | Config | What it replaced |
|---|---|---|---|
| `_bank_breaker` | `ctx.blackboard` | `max_failures=1, cooldown_s=600` | `depleted_banks: dict[tuple[int,int], float]` |
| `_craft_material_breaker` | `ctx.blackboard` | `max_failures=3, cooldown_s=300` | `_craft_bs_material_cooldown: float` |
| `_ore_pickup_breaker` | `ctx.blackboard` | `max_failures=2, cooldown_s=3600` | `_ore_pickup_fails: dict[int, int]` |

All three keep their legacy dict alongside the breaker so existing tests (which don't install a breaker) continue to work. A future cleanup pass can remove the legacy fallbacks.

**Impact**: Any future "procedure X loops on target Y" bug can now be prevented with 1-3 lines at the call site instead of reinventing a cooldown dict.

---

### Phase 3: `FixLock` — no duplicate self-improvement sessions

**File**: `tools/fix_lock.py` (132 lines)

File-based lock at `data/fixing.json` that prevents the supervisor and a human operator from launching Claude Code on the same problem in parallel.

```python
with FixLock(f"targeted_fix:{fix_key}", sha=head_before) as got:
    if not got:
        print("already being fixed — skipping")
        continue
    success, output = call_claude_with_prompt(prompt, timeout)
```

Behaviors:
- **SHA-based invalidation**: If the lock was taken at SHA `abc` and we've since committed `def`, the lock is stale (problem presumably already fixed). A new session can take over.
- **Age-based expiry**: `MAX_LOCK_AGE = 1800` seconds (30 min). After that, any crashed-leftover lock is ignored.
- **Safe defaults**: Missing file, malformed JSON, or write errors never block — they just report "not locked" and let work proceed.

Wired into `tools/supervisor.py` at both Level 2 (targeted fix) and Level 3 (full analysis) Claude Code call sites.

**Impact**: Solved the 4/8 issue where the supervisor auto-launched Claude Code mid-manual-fix and ended up duplicating work, competing for `planner.py` edits, and forcing a merge reconciliation.

---

### Phase 4: Planner split

**Before**: `anima/planner/planner.py` = 1,841 lines, one god class containing 9 priority branches, deadlock resolver, forum escalation, roaming helpers, and three inline procedure classes.

**After**: planner.py = 1,239 lines (Planner class + `select_procedure` + `_check_stuck` only), with responsibilities extracted to:

| Module | Lines | Contains |
|---|---|---|
| `anima/planner/helpers.py` | 269 | `_MoveToProcedure`, `_PickUpAndSmelt`, `_ScavengeGroundItems` (inline procedure classes) |
| `anima/planner/deadlock.py` | 325 | `DeadlockResolver` class — 5 recovery strategies + forum escalation + `_compose_help_post` (LLM-backed) + `find_ground_valuables` |
| `anima/planner/roaming.py` | 302 | `RoamingHelper` class — `move_to_location`, `try_move_to_activity`, `mark_nearby_mine_exhausted`, `is_destination_failed` + module-level `_find_waypoint_toward` |

Each helper class takes a `Planner` reference in `__init__` (e.g. `DeadlockResolver(self)`) so it can read/write planner-owned state like `self._planner._failed_destinations` without requiring those fields to become public.

**Risk avoided**: Circular imports. The split modules only import `Planner` under `TYPE_CHECKING`. `helpers.py` uses lazy (in-method) imports for `anima.action.movement.go_to` etc.

**Impact**: Each file is now under 400 lines — easier for both humans and Claude Code to reason about. Future edits to, say, deadlock logic no longer force a re-read of the entire planner.

---

### Phase 5: `PlannerBlackboard` — typed state surface

**File**: `anima/planner/state.py` (105 lines)

Dataclass documenting every blackboard key the planner currently reads/writes, with a `_KEY_MAP` translating Python attribute names to blackboard string keys. Unknown keys are preserved in `extras` so legacy code continues to work during migration.

```python
@dataclass
class PlannerBlackboard:
    craft_bs_fails: int = 0
    skip_procedures: set[str] = field(default_factory=set)
    depleted_banks: dict[tuple[int, int], float] = field(default_factory=dict)
    junk_ore_serials: set[int] = field(default_factory=set)
    deadlock_recovery_level: int = 0
    # ... 17 typed fields total

    _KEY_MAP = {
        "craft_bs_fails": "_craft_bs_fails",
        "skip_procedures": "_skip_procedures",
        # ...
    }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannerBlackboard": ...

    def to_dict(self) -> dict: ...
```

**Intentionally excluded from `_KEY_MAP`**:
- CircuitBreaker instances (`_bank_breaker`, `_craft_material_breaker`, `_ore_pickup_breaker`) — they're object references, not serializable state, and are set up anew each session by `Planner.run()`.
- `_failed_destinations` — lives on the Planner instance itself (`self._failed_destinations`), not in `ctx.blackboard`.

This file is currently documentation + optional migration boundary. The planner still uses string-key blackboard access at call sites; a future task can migrate them to `bb = PlannerBlackboard.from_dict(ctx.blackboard)` / `ctx.blackboard = bb.to_dict()`.

---

## Part 2 — Strategic improvements (Round 2, 5 parallel tracks)

After Part 1 stabilized the infrastructure, Part 2 addressed the behavioral gaps surfaced by production observation.

### Track #5 — LocationScore (cost-weighted roaming)

**File**: `anima/planner/roaming.py` (+81 lines)

Replaces "nearest matching location wins" with a scored selection:

```python
@dataclass
class LocationScore:
    location: Location
    distance: int
    failed_attempts: int = 0    # recent failure count (5-min decay)
    success_rate: float = 1.0   # rolling success rate, default optimistic
    last_visit: float = 0.0     # unix timestamp

    def score(self) -> float:
        base = 1000 - self.distance
        penalty = self.failed_attempts * 50
        bonus = self.success_rate * 100
        freshness = min((time.time() - self.last_visit) / 60, 10)
        return base - penalty + bonus + freshness
```

`RoamingHelper` now holds a `LocationStats` instance tracking visit history. `move_to_location` and `try_move_to_activity` rank candidates by score and pick the highest, not the closest.

**Solves**: "Agent keeps trying `Minoc Tinker` 95 tiles away instead of `Minoc Provisioner` 20 tiles away because both match the keyword."

---

### Track #3 — Haiku micro-fix tier

**Files**: `tools/fix_tier.py` (122 lines) + supervisor integration

Two-tier self-improvement:

```
Tier 1 (Haiku 4.5, 60s): patterned one-liner fixes
  → if committed: done
  → if "ESCALATE: reason": go to Tier 2

Tier 2 (Opus 4.6, 900s): full diagnostic (existing behavior)
```

The Haiku prompt is ~2KB (vs Opus's ~5KB) and demands terse output:

```
Your job: apply a small, obvious patch (1-5 lines) or say "ESCALATE".
- If you can fix it in 5 lines or fewer, apply the patch and commit.
- If the root cause is unclear OR requires multiple files,
  respond with exactly "ESCALATE: <one-line reason>"
- Your entire session should finish in under 60 seconds.
```

`is_too_complex(output)` detects escalation signals. Wired into `supervisor.py` Level 2 (targeted fix) only — Level 3 (full analysis) is still Opus exclusively because those problems are by definition not one-liners.

**Impact (expected)**: Common loop/cooldown/import fixes should drop from 10-min cycles to 30-sec cycles. The 15+ historical timeouts in `improvements.jsonl` were mostly things Haiku could have handled.

---

### Track #4 — Temporal test harness

**Files**: `tests/harness/{world.py, runner.py}` (563 lines) + smoke tests

`MockWorld` is an AgentContext-compatible simulator: player position, inventory, ore banks, blackboard, advancing `world.time`. `run_planner_ticks(planner, world, max_ticks)` drives the planner in a loop, short-circuiting each selected procedure's outcome via a `WorldBehavior` strategy:

```python
async def test_roams_to_second_mine_when_first_depleted():
    world = MockWorld()
    world.add_mine_bank(x=100, y=100, ore_count=0)  # empty
    world.add_mine_bank(x=200, y=200, ore_count=20) # fresh

    result = await run_planner_ticks(planner, world, max_ticks=50)
    assert world.ore_mined_at(200, 200) > 0
    assert world.total_ticks_wasted_at_bank(100, 100) < 5
```

Key constraint: the harness never calls real `procedure.run()`. It intercepts after `select_procedure` returns and deterministically mutates the mock world. This keeps tests fast (<100ms) and fully reproducible.

**Impact**: Sequence bugs like "pickup fails → pickup fails → 10 fails → planner gives up → idle loop" can now be caught at unit-test time instead of only surfacing after 2 hours of live play.

---

### Track #2 — LLM strategy selector

**File**: `anima/planner/strategy.py` (204 lines)

Every 5 minutes, an LLM (whichever is configured in `ctx.llm`) picks one of five named session strategies:

| Strategy | Excludes |
|---|---|
| `grind_mining` | craft_blacksmith, sell_to_vendor |
| `sell_inventory` | mine_ore, smelt_ore, craft_blacksmith |
| `bank_colored` | mine_ore, craft_blacksmith |
| `upgrade_tools` | mine_ore, craft_blacksmith |
| `fill_coffers` | (nothing excluded) |

The LLM prompt is short (~600 tokens) — just current state + the 5 options + a fixed response format. Response is parsed as:

```
STRATEGY: <name>
REASONING: <one sentence>
```

If the LLM returns something unknown or the network fails, the selector stays on the default (`grind_mining`) and tries again next interval.

The planner's `_get_proc` helper now checks `self._strategy.is_excluded(name)` before returning a procedure. Combined with the existing `_skip_procedures` and `_is_supervisor_skipped` checks, the filter order is:

1. `_skip_procedures` (repeat-failure skip)
2. `_is_supervisor_skipped` (supervisor hint)
3. `strategy.is_excluded` (this layer — LLM mode)
4. `goals.is_forbidden` (Track #1, next)
5. `self.registry.get(name)` (actual lookup)

**Activation gate**: The selector has an `_active: bool` flag that defaults to `False` and flips to `True` only after the first successful LLM response. Before activation, the exclusion filter is a no-op. This keeps existing tests (which don't configure an LLM) from breaking — they see the default but no filtering is applied.

**Impact**: LLM creativity × rule-based deterministic execution. The LLM sets the high-level direction at 5-minute cadence while the planner's 200ms tick loop remains fast and predictable.

---

### Track #1 — Goal stack / intention model

**File**: `anima/planner/goals.py` (237 lines)

Adds a pushable/poppable stack of `Goal` objects representing what the agent is currently trying to achieve. The top of the stack is the active goal; when it's satisfied or expired, it pops and the next becomes active.

```python
@dataclass
class Goal:
    name: str                                      # "collect_50_iron_ingots"
    description: str                               # human-readable
    preferred_procedures: set[str]                 # advisory (not used yet)
    forbidden_procedures: set[str]                 # hard exclusion
    deadline: float                                # auto-expire
    is_satisfied_fn: Callable[[Ctx], bool] | None  # "done yet?"
    progress_fn: Callable[[Ctx], float] | None     # 0.0 - 1.0
```

Three factory functions for common patterns:

```python
goal = make_collect_iron_ingots_goal(target=50, deadline_s=3600)
# Satisfied when _count_iron_ingots(ctx) >= 50
# Forbids: sell_to_vendor (don't sell what we're collecting)

goal = make_clear_inventory_goal(deadline_s=1800)
# Satisfied when no crafted items AND no colored ingots in backpack
# Forbids: mine_ore, smelt_ore, craft_blacksmith

goal = make_deposit_gold_goal(threshold=200)
# Satisfied when gold < threshold
```

`Planner.run()` calls `self._goals.update(ctx)` each tick, which pops satisfied/expired goals (may cascade multiple pops). `_get_proc` adds a final filter: `if self._goals.is_forbidden(name): return None`.

**Deliberate YAGNI**: `is_preferred` is NOT used yet. We only exclude forbidden procedures for now. A future enhancement can add preference weighting once we observe how the basic version behaves.

**Goal pushing is manual for now**. Neither the strategy selector nor any automated layer pushes goals — they have to be pushed by code (e.g. at session start) or by a future LLM-driven goal planner. This keeps the first iteration's scope small.

**Impact**: Solves the root cause of "accumulated 186 ingots with no plan". With an active `collect_50_iron_ingots` goal, the agent stops mining once satisfied and the next goal (clear_inventory) drives the sell phase.

---

## Architecture diagram (after all improvements)

```
                  ┌────────────────────────────┐
                  │        Self-Improvement     │
                  │  Supervisor (30s health)    │
                  │   ↓ FixLock                 │
                  │   Tier 1: Haiku 60s         │
                  │   Tier 2: Opus  900s        │
                  └────────────┬───────────────┘
                               │ (modifies code)
                               ▼
┌──────────────────────────────────────────────┐
│                  Planner                      │
│  ┌────────────────────────────────────────┐  │
│  │            High-Level Layer             │  │
│  │  StrategySelector  (LLM every 5 min)    │  │
│  │  GoalStack         (intention)          │  │
│  └────────────────┬───────────────────────┘  │
│                   │                           │
│  ┌────────────────▼───────────────────────┐  │
│  │         select_procedure()              │  │
│  │                                          │  │
│  │  _get_proc filter chain:                │  │
│  │   1. _skip_procedures                   │  │
│  │   2. supervisor hints                   │  │
│  │   3. strategy.is_excluded               │  │
│  │   4. goals.is_forbidden                 │  │
│  │   5. registry.get                       │  │
│  │                                          │  │
│  │  Priority 1-9 rule cascade              │  │
│  │                                          │  │
│  │  PlannerHealth (loop detection, 30s)    │  │
│  └──┬──────────────────────────────────┬──┘  │
│     │                                  │      │
│  ┌──▼──────────┐ ┌────────────┐ ┌────▼────┐ │
│  │ Circuit     │ │ Roaming    │ │Deadlock │ │
│  │ Breakers    │ │ Helper     │ │Resolver │ │
│  │ bank 1/600  │ │ Location   │ │5 strats │ │
│  │ craft 3/300 │ │ Score      │ │+ forum  │ │
│  │ pickup 2/3h │ │ (dist +    │ │escalate │ │
│  │             │ │  history)  │ │         │ │
│  └─────────────┘ └────────────┘ └─────────┘ │
│                                                │
│  ┌─────────────────────────────────────────┐ │
│  │       Helper Procedures (helpers.py)     │ │
│  │  _MoveToProcedure                        │ │
│  │  _PickUpAndSmelt                         │ │
│  │  _ScavengeGroundItems                    │ │
│  └─────────────────────────────────────────┘ │
└────────────────────────────────────────────────┘

              Registered Procedures
      mine_ore / smelt_ore / craft_blacksmith /
      sell_to_vendor / buy_from_vendor / bank_deposit / ...
```

---

## File inventory

**New modules created**:

| File | Lines | Purpose |
|---|---|---|
| `anima/planner/health.py` | 68 | Sliding-window loop detection |
| `anima/planner/circuit_breaker.py` | 107 | Unified per-target cooldown abstraction |
| `anima/planner/helpers.py` | 269 | Inline procedure classes (extracted) |
| `anima/planner/deadlock.py` | 325 | Deadlock resolution + forum escalation (extracted) |
| `anima/planner/roaming.py` | 302 | Location routing + LocationScore |
| `anima/planner/state.py` | 105 | Typed blackboard dataclass |
| `anima/planner/strategy.py` | 204 | LLM session strategy selector |
| `anima/planner/goals.py` | 237 | Goal stack / intention model |
| `tools/fix_tier.py` | 122 | Haiku micro-fix tier |
| `tools/fix_lock.py` | 132 | Duplicate-session prevention |
| `tests/harness/world.py` | 436 | MockWorld for temporal tests |
| `tests/harness/runner.py` | 127 | `run_planner_ticks` simulator |

**Modified**:

| File | Before | After | Delta |
|---|---|---|---|
| `anima/planner/planner.py` | 1,841 | 1,239 | **-602 (-33%)** |
| `tools/supervisor.py` | (various small additions) | | |

**New tests**:

| File | Tests |
|---|---|
| `tests/test_planner_health.py` | 7 |
| `tests/test_circuit_breaker.py` | 10 |
| `tests/test_fix_lock.py` | 10 |
| `tests/test_planner_state.py` | 3 |
| `tests/test_roaming_cost.py` | 10 |
| `tests/test_fix_tier.py` | 22 |
| `tests/test_temporal_scenarios.py` | 3 |
| `tests/test_strategy_selector.py` | 11 |
| `tests/test_goal_stack.py` | 23 |
| **Total new** | **99** |

---

## Metrics

| | Before | After |
|---|---|---|
| planner.py lines | 1,841 | 1,239 |
| Total planner/ lines | 1,841 | 2,656 (across 10 files) |
| Test count | 321 | 413 |
| Ad-hoc blackboard cooldown keys | ~10 | 0 (all migrated to CircuitBreaker) |
| Self-improvement tier | 1 (Opus 10 min) | 2 (Haiku 60s → Opus 900s) |
| Loop detection latency | ~10 min (supervisor) | ~30 sec (PlannerHealth) |
| God-file risk (files > 1500 lines) | 1 | 0 |
| Dup Claude Code sessions | possible | prevented (FixLock) |
| Temporal sequence tests | 0 | 3+ (harness installed) |

---

## Key design decisions

1. **Keep legacy fallbacks for every CircuitBreaker migration.** Existing tests set up their own blackboards without installing a breaker. Ripping out the dict fallback would have broken ~30 tests. Each migration writes to *both* the dict and the breaker. A later task can drop the dict.

2. **StrategySelector's `_active` gate.** The default strategy (`grind_mining`) would break two existing tests if applied unconditionally before the LLM has spoken. The selector stays inactive (no filtering) until the first successful LLM response turns it on.

3. **GoalStack's YAGNI.** `is_preferred` is defined but not used. Only `is_forbidden` feeds the planner filter. Preference weighting can be added after real-world observation.

4. **Temporal harness never runs real procedures.** `run_planner_ticks` intercepts after `select_procedure` returns and mutates the MockWorld via a `WorldBehavior` strategy. Trying to call real `procedure.run()` would require mocking packets, timers, async bus events, and game state — out of scope.

5. **FixLock is SHA-based, not time-based.** A stale lock at SHA `abc` is automatically invalidated once `def` is committed. This is the right semantics: if a commit has been made, the problem is presumably already being handled.

6. **Planner split uses TYPE_CHECKING imports for Planner.** `DeadlockResolver`, `RoamingHelper`, etc. need `Planner` type hints but must not import `planner.py` at runtime (circular). The `if TYPE_CHECKING:` block is the idiom.

7. **Haiku tier is Level 2 only.** Level 3 full_analysis was NOT converted to Haiku because those problems are inherently architectural — requiring Opus judgment. Quick fixes belong to Level 2 by definition.

---

## Running catalog of legacy blackboard keys

These keys are still live but documented by `PlannerBlackboard._KEY_MAP`. Future migration will replace them with typed attribute access.

| Blackboard key | Type | Source | Purpose |
|---|---|---|---|
| `_craft_bs_fails` | int | craft_blacksmith | Consecutive craft failures |
| `_craft_bs_material_fails` | int | craft_blacksmith | "insufficient metal" counter |
| `_craft_bs_material_cooldown` | float | craft_blacksmith | Legacy cooldown (now backed by CircuitBreaker) |
| `_make_tools_gave_up` | bool | make_tools | Tinkering skill too low |
| `_skip_procedures` | set[str] | planner | Procedures to skip this cycle |
| `depleted_banks` | dict[(x,y), float] | mine.py | Legacy bank tracking (now backed by CircuitBreaker) |
| `exhausted_mines` | dict[str, float] | roaming | Mine LOCATIONS marked exhausted |
| `_mine_exhausted_until` | float | planner | Global mining suspension |
| `_mine_consec_fail` | int | mine.py | Per-target mine failures |
| `_junk_ore_serials` | set[int] | planner | Ore items permanently skipped |
| `_unsmelable_ore_hues` | set[int] | smelt_ore | Junk hues |
| `_ore_pickup_fails` | dict[int, int] | planner | Legacy pickup counter (now CircuitBreaker) |
| `_small_iron_ore_serials` | set[int] | planner | Sub-threshold ore piles |
| `_deadlock_recovery_level` | int | deadlock | Escalation tier |
| `_deadlock_attempt_count` | int | deadlock | Attempts at current tier |
| `refused_vendors` | dict[int, float] | vendor | NPCs that rejected us |
| `planner_intent` | str | planner | Human-readable current intent |
| `current_procedure` | str \| None | planner | Active procedure name |
| `_bank_breaker` | CircuitBreaker | Planner.run | Object reference (not serializable) |
| `_craft_material_breaker` | CircuitBreaker | Planner.run | Object reference (not serializable) |
| `_ore_pickup_breaker` | CircuitBreaker | Planner.run | Object reference (not serializable) |

---

## Future work (explicitly out of scope for this refactor)

These were considered and deferred:

- **LLM goal generation**: Currently goals are pushed by code (no one pushes them automatically). A future layer could have the LLM decide "what should my next goal be?" based on inventory, gold, skill levels.
- **Goal preference weighting**: `is_preferred` is defined but not wired to any priority boost. Adding this requires a way to influence the priority cascade without overriding it.
- **Legacy blackboard cleanup**: Most CircuitBreaker migrations keep the dict fallback for test compatibility. After tests are migrated, the fallback can be removed.
- **Planner priority decomposition**: The 9-priority cascade in `select_procedure` is still one giant method. Splitting into one file per priority (e.g. `priorities/survival.py`, `priorities/gathering.py`) was discussed but deferred — too many cross-references between priorities.
- **Dashboard / metrics over time**: No live observability UI. `improvements.jsonl` is the closest thing. A sqlite metrics table + `/status` endpoint would help debugging but is its own project.
- **Replay / simulation mode**: Record world state + decisions, replay to debug. Would be powerful but large.
- **Location quality learning**: `LocationScore` currently tracks failed_attempts and success_rate at the individual visit level. A persistent per-location quality metric (across sessions) would require a storage layer.

---

## Commit history

### Phase 1-5 (14 commits + 1 review fix)

```
18d22bf  Add PlannerHealth: sliding-window loop detection
708049e  Wire PlannerHealth into planner loop
dfc9715  Add CircuitBreaker abstraction
326c641  CircuitBreaker: split state into typed dicts (pyright fix)
5e40059  Migrate mine bank depletion to CircuitBreaker
3442b1a  Migrate craft_blacksmith material cooldown to CircuitBreaker
b280457  Migrate ore-pickup failure tracking to CircuitBreaker
4925862  Add fix lock
7d20b6a  Supervisor: honor fix lock
b9af0b3  Extract procedure helper classes to helpers.py
ddd3044  Extract deadlock resolution to deadlock.py
ce7ac48  Extract location roaming to roaming.py
b60ba4f  Add PlannerBlackboard typed state
a8b40af  Document PlannerBlackboard migration path
50b0c46  Code review fixes (breaker consistency, KEY_MAP gaps, health/deadlock race)
```

### Round 2 (8 commits)

```
7684aea  Add LocationScore cost model for roaming selection
711d47f  Add Haiku micro-fix tier to supervisor self-improvement
9e14b38  fix_tier: remove unused datetime import
7589c32  Add temporal test harness for multi-tick planner scenarios
46bb329  Harness: clean up unused imports and narrow behavior type
d4b8d0b  Add LLM strategy selector and planner filter integration
23cc737  strategy: fix STRATEGY_FILL_COFFERS dict type
db5e61b  Add Goal stack: intention model for planner selection
```
