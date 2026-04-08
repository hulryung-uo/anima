# Planner Architectural Improvements

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the Anima planner to prevent the recurring "fix X loop" commit pattern (35+ such commits in one session) by introducing proper circuit-breaker abstractions, faster loop detection, and better state organization.

**Architecture:** Incremental, TDD-driven refactoring in 5 phases. Phase 1 adds fast loop detection so future bugs surface quickly. Phase 2 adds a unified `CircuitBreaker` abstraction and migrates existing ad-hoc cooldown logic to use it. Phase 3 prevents duplicate Claude Code sessions. Phase 4 splits `planner.py` (1841 lines) into focused modules. Phase 5 adds a typed blackboard so stale-key bugs are caught statically.

**Tech Stack:** Python 3.12+, pytest + pytest-asyncio, dataclasses

---

## Why this plan

Analysis of 55 commits made during a single debugging session surfaced these patterns:

- **35 commits were "Fix X loop"** — almost every bug was "priority keeps being selected while state doesn't change"
- **planner.py is 1841 lines** — every fix touches the same file; Claude Code reads the entire file each time (wastes time, hits timeouts)
- **46 different blackboard key accesses in planner.py alone** — state management is ad-hoc and uncoordinated
- **False DEADLOCK detection** from stale `state.json` wasted multiple Claude Code cycles
- **Duplicate fixes** when the supervisor launched Claude Code on the same problem we were already fixing manually

The improvements in this plan target the **root causes**, not individual loops.

## File Structure

| Phase | File | Action | Responsibility |
|-------|------|--------|---------------|
| 1 | `anima/planner/health.py` | Create | Planner diversity metric, fast loop detection |
| 1 | `anima/planner/planner.py:run` | Modify | Call health check each tick |
| 1 | `tests/test_planner_health.py` | Create | Health metric tests |
| 2 | `anima/planner/circuit_breaker.py` | Create | Unified cooldown/retry abstraction |
| 2 | `anima/planner/planner.py` | Modify | Migrate ad-hoc cooldowns to use CircuitBreaker |
| 2 | `anima/skills/gathering/mine.py` | Modify | Migrate `depleted_banks` to CircuitBreaker |
| 2 | `anima/procedures/mine_ore.py` | Modify | Migrate bank tracking to CircuitBreaker |
| 2 | `anima/procedures/craft_blacksmith.py` | Modify | Migrate `_craft_bs_material_cooldown` to CircuitBreaker |
| 2 | `tests/test_circuit_breaker.py` | Create | CircuitBreaker tests |
| 3 | `tools/fix_lock.py` | Create | File-based fix lock helpers |
| 3 | `tools/supervisor.py` | Modify | Honor fix lock before Claude Code call |
| 3 | `tests/test_fix_lock.py` | Create | Fix lock tests |
| 4 | `anima/planner/helpers.py` | Create | `_MoveToProcedure`, `_PickUpAndSmelt`, `_ScavengeGroundItems` |
| 4 | `anima/planner/deadlock.py` | Create | `_resolve_deadlock`, `_escalate_to_forum` |
| 4 | `anima/planner/planner.py` | Modify | Shrinks to `run()` + `select_procedure()` + priority dispatch |
| 5 | `anima/planner/state.py` | Create | `PlannerBlackboard` dataclass |
| 5 | `anima/planner/planner.py` | Modify | Use typed blackboard |
| 5 | `tests/test_planner_state.py` | Create | BlackboardState tests |

---

# Phase 1: Planner Health Metrics

**Goal:** Detect loop patterns in under 30 seconds (down from ~10 minutes via the supervisor analysis cycle), so the planner stops itself before it wastes time.

## Task 1.1: Create planner health module

**Files:**
- Create: `anima/planner/health.py`
- Create: `tests/test_planner_health.py`

- [ ] **Step 1.1.1: Write the failing test**

Create `tests/test_planner_health.py`:

```python
"""Tests for planner health metrics."""
from anima.planner.health import PlannerHealth


class TestPlannerHealth:
    def test_initial_state_is_healthy(self):
        h = PlannerHealth(window=20)
        assert h.is_looping() is False

    def test_low_diversity_detected(self):
        """20 selections of the same procedure → loop detected."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(20):
            h.record("mine_ore")
        assert h.is_looping() is True
        assert h.dominant_procedure() == "mine_ore"

    def test_normal_rotation_not_looping(self):
        """A healthy mine→smelt→craft→sell rotation is not a loop."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for _ in range(5):
            for p in ("mine_ore", "smelt_ore", "craft_blacksmith", "sell_to_vendor"):
                h.record(p)
        assert h.is_looping() is False

    def test_window_slides(self):
        """Old entries drop off the window."""
        h = PlannerHealth(window=5, min_diversity=0.5)
        for _ in range(5):
            h.record("mine_ore")
        assert h.is_looping() is True
        # Record 5 new diverse entries — loop should clear
        for p in ("a", "b", "c", "d", "e"):
            h.record(p)
        assert h.is_looping() is False

    def test_not_enough_data_not_looping(self):
        """Need at least window//2 entries before reporting a loop."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        h.record("mine_ore")
        h.record("mine_ore")
        assert h.is_looping() is False  # only 2 entries, not enough

    def test_dominant_procedure_none_when_empty(self):
        h = PlannerHealth(window=20)
        assert h.dominant_procedure() is None

    def test_record_skip_doesnt_count(self):
        """record_skip tracks skips separately without polluting diversity."""
        h = PlannerHealth(window=20, min_diversity=0.2)
        for p in ("a", "b", "c", "d"):
            h.record(p)
        for _ in range(50):
            h.record_skip("mine_ore")  # doesn't affect is_looping
        assert h.is_looping() is False
```

- [ ] **Step 1.1.2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anima.planner.health'`

- [ ] **Step 1.1.3: Implement PlannerHealth**

Create `anima/planner/health.py`:

```python
"""Planner health metrics — fast loop detection.

Tracks the last N procedure selections in a sliding window and flags
a loop when the diversity (unique / total) drops below a threshold.

This is deliberately simple and synchronous — the planner calls
`record()` after each selection and checks `is_looping()` to decide
whether to break out of a suspected infinite loop.
"""

from __future__ import annotations

from collections import Counter, deque


class PlannerHealth:
    """Sliding-window planner selection tracker.

    Reports a loop when fewer than `min_diversity` fraction of the last
    `window` selections were unique.
    """

    def __init__(self, window: int = 20, min_diversity: float = 0.2) -> None:
        self._window = window
        self._min_diversity = min_diversity
        self._selections: deque[str] = deque(maxlen=window)
        self._skip_counts: Counter[str] = Counter()

    def record(self, procedure: str) -> None:
        """Record a procedure that was actually selected and run."""
        self._selections.append(procedure)

    def record_skip(self, procedure: str) -> None:
        """Record a procedure that was filtered out.

        Separate counter so spammy skips don't look like loop activity
        but we still know which procedures the planner keeps rejecting.
        """
        self._skip_counts[procedure] += 1

    def is_looping(self) -> bool:
        """True if the recent selection window shows low diversity."""
        total = len(self._selections)
        if total < self._window // 2:
            return False  # not enough data yet
        unique = len(set(self._selections))
        return (unique / total) < self._min_diversity

    def dominant_procedure(self) -> str | None:
        """Most-selected procedure in the current window, or None."""
        if not self._selections:
            return None
        return Counter(self._selections).most_common(1)[0][0]

    def reset(self) -> None:
        """Clear all state (used after a successful recovery)."""
        self._selections.clear()
        self._skip_counts.clear()

    def snapshot(self) -> dict:
        """Return a dict snapshot for logging / diagnostics."""
        return {
            "window_size": len(self._selections),
            "unique": len(set(self._selections)),
            "dominant": self.dominant_procedure(),
            "looping": self.is_looping(),
            "top_skips": dict(self._skip_counts.most_common(5)),
        }
```

- [ ] **Step 1.1.4: Run tests to verify they pass**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner_health.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 1.1.5: Commit**

```bash
git add anima/planner/health.py tests/test_planner_health.py
git commit -m "Add PlannerHealth: sliding-window loop detection"
```

## Task 1.2: Wire PlannerHealth into planner loop

**Files:**
- Modify: `anima/planner/planner.py` (Planner.__init__ and Planner.run)

- [ ] **Step 1.2.1: Read the current planner init and run loop**

Read `anima/planner/planner.py` lines 37-108 to understand the current structure:
- `__init__` creates `self.continuation_hint`, `self._running`, `self._repeat_counter`, etc.
- `run()` calls `tick()` and handles the result

- [ ] **Step 1.2.2: Add PlannerHealth to __init__**

In `anima/planner/planner.py`, modify `Planner.__init__` to add the health tracker:

```python
# At the top of the file, add the import near other anima.planner imports
from anima.planner.health import PlannerHealth

# In __init__, after self._last_escalation = 0.0, add:
        self._health = PlannerHealth(window=30, min_diversity=0.2)
        self._health_break_until: float = 0.0  # pause selection after detected loop
```

- [ ] **Step 1.2.3: Record selections in the run loop**

In `Planner.run()`, after `result = await self.tick(ctx)` and before the existing repeat-failure tracking block, add:

```python
                # Record selection for health metrics (if anything was picked)
                proc_name_for_health = (
                    getattr(result, "_proc_name", "")
                    or self._last_procedure
                )
                if proc_name_for_health:
                    self._health.record(proc_name_for_health)

                # Check for loop pattern — if detected, pause selection
                # for 60 seconds and clear the repeat counter so the
                # deadlock resolver / auto-recover can intervene.
                if (self._health.is_looping()
                        and time.time() > self._health_break_until):
                    dominant = self._health.dominant_procedure()
                    logger.warning(
                        "planner_health_loop_detected",
                        dominant=dominant,
                        snapshot=self._health.snapshot(),
                    )
                    self._health_break_until = time.time() + 60.0
                    self._health.reset()
                    # Force a fresh selection path next tick
                    self.continuation_hint = None
                    self._repeat_counter.clear()
```

- [ ] **Step 1.2.4: Respect the health break in tick()**

In `Planner.tick()`, at the very top (before `proc = await self.select_procedure(ctx)`), add:

```python
        # Planner health break — after a loop was detected we pause
        # selection briefly so the environment has a chance to change.
        if time.time() < self._health_break_until:
            return None
```

- [ ] **Step 1.2.5: Run all tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 312+ tests PASS.

- [ ] **Step 1.2.6: Commit**

```bash
git add anima/planner/planner.py
git commit -m "Wire PlannerHealth into planner loop for fast loop detection"
```

---

# Phase 2: CircuitBreaker Abstraction

**Goal:** Replace 46+ ad-hoc cooldown/retry blackboard keys with a single reusable abstraction so new loops can be prevented in one line.

## Task 2.1: Create CircuitBreaker class

**Files:**
- Create: `anima/planner/circuit_breaker.py`
- Create: `tests/test_circuit_breaker.py`

- [ ] **Step 2.1.1: Write the failing tests**

Create `tests/test_circuit_breaker.py`:

```python
"""Tests for the CircuitBreaker abstraction."""
import time
from anima.planner.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        assert cb.is_open("x") is False

    def test_opens_after_max_failures(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        assert cb.is_open("target_a") is False
        cb.record_failure("target_a")
        cb.record_failure("target_a")
        assert cb.is_open("target_a") is False  # still below threshold
        cb.record_failure("target_a")
        assert cb.is_open("target_a") is True

    def test_different_targets_independent(self):
        cb = CircuitBreaker(max_failures=2, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is True
        assert cb.is_open("b") is False

    def test_cooldown_expires(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=0.05)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        time.sleep(0.06)
        assert cb.is_open("a") is False  # cooldown expired

    def test_record_success_resets(self):
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("a")
        cb.record_success("a")
        assert cb.failure_count("a") == 0
        cb.record_failure("a")
        cb.record_failure("a")
        assert cb.is_open("a") is False  # needed 3 after reset

    def test_open_targets_lists_active(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("b")
        assert set(cb.open_targets()) == {"a", "b"}

    def test_trip_once_opens_immediately(self):
        """trip() skips counting and opens the breaker right away."""
        cb = CircuitBreaker(max_failures=3, cooldown_s=60.0)
        cb.trip("a")
        assert cb.is_open("a") is True

    def test_reset_target(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        assert cb.is_open("a") is True
        cb.reset("a")
        assert cb.is_open("a") is False

    def test_reset_all(self):
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure("a")
        cb.record_failure("b")
        cb.reset_all()
        assert cb.is_open("a") is False
        assert cb.is_open("b") is False

    def test_hashable_targets(self):
        """Targets can be tuples, ints, strings — anything hashable."""
        cb = CircuitBreaker(max_failures=1, cooldown_s=60.0)
        cb.record_failure((10, 20))
        cb.record_failure(42)
        cb.record_failure("name")
        assert cb.is_open((10, 20)) is True
        assert cb.is_open(42) is True
        assert cb.is_open("name") is True
```

- [ ] **Step 2.1.2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_circuit_breaker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anima.planner.circuit_breaker'`

- [ ] **Step 2.1.3: Implement CircuitBreaker**

Create `anima/planner/circuit_breaker.py`:

```python
"""Unified circuit-breaker / cooldown abstraction for planner state.

Replaces ad-hoc `depleted_banks`, `refused_vendors`, `_failed_destinations`,
`_craft_bs_material_cooldown`, `_ore_pickup_fails`, etc. patterns that
were scattered throughout the planner and its procedures.

Usage:
    breaker = CircuitBreaker(max_failures=3, cooldown_s=600)
    if not breaker.is_open(target_serial):
        result = await attempt(target_serial)
        if result.success:
            breaker.record_success(target_serial)
        else:
            breaker.record_failure(target_serial)
"""

from __future__ import annotations

import time
from typing import Any, Hashable


class CircuitBreaker:
    """Track failures per target and cool down after a threshold is hit.

    Each target is counted independently. When a target reaches
    `max_failures`, it becomes "open" for `cooldown_s` seconds, during
    which `is_open(target)` returns True. After cooldown expires the
    counter auto-resets.
    """

    def __init__(self, max_failures: int, cooldown_s: float) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be > 0")
        self._max = max_failures
        self._cooldown = cooldown_s
        # target -> [failure_count, tripped_at]
        self._state: dict[Hashable, list[float]] = {}

    def record_failure(self, target: Hashable) -> None:
        """Count one failure. Opens the breaker if max_failures reached."""
        entry = self._state.setdefault(target, [0, 0.0])
        entry[0] += 1
        if entry[0] >= self._max:
            entry[1] = time.time()

    def record_success(self, target: Hashable) -> None:
        """Reset counter and cooldown for a target."""
        self._state.pop(target, None)

    def trip(self, target: Hashable) -> None:
        """Open the breaker immediately, skipping the counter."""
        self._state[target] = [self._max, time.time()]

    def reset(self, target: Hashable) -> None:
        """Remove a target from tracking entirely."""
        self._state.pop(target, None)

    def reset_all(self) -> None:
        self._state.clear()

    def is_open(self, target: Hashable) -> bool:
        """True while the target is in its cooldown window."""
        entry = self._state.get(target)
        if not entry:
            return False
        count, tripped_at = entry
        if count < self._max:
            return False
        if time.time() - tripped_at >= self._cooldown:
            # Auto-expire
            self._state.pop(target, None)
            return False
        return True

    def failure_count(self, target: Hashable) -> int:
        entry = self._state.get(target)
        return entry[0] if entry else 0

    def open_targets(self) -> list[Hashable]:
        """List of targets whose breaker is currently open."""
        return [t for t in list(self._state.keys()) if self.is_open(t)]

    def snapshot(self) -> dict[str, Any]:
        """Diagnostic snapshot for logging."""
        now = time.time()
        return {
            "max_failures": self._max,
            "cooldown_s": self._cooldown,
            "tracked": len(self._state),
            "open": [
                {
                    "target": str(t),
                    "count": self._state[t][0],
                    "open_for_more_s": max(
                        0.0, self._cooldown - (now - self._state[t][1])
                    ),
                }
                for t in self.open_targets()
            ],
        }
```

- [ ] **Step 2.1.4: Run tests to verify they pass**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_circuit_breaker.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 2.1.5: Commit**

```bash
git add anima/planner/circuit_breaker.py tests/test_circuit_breaker.py
git commit -m "Add CircuitBreaker: unified per-target cooldown abstraction"
```

## Task 2.2: Migrate mining bank depletion to CircuitBreaker

**Files:**
- Modify: `anima/skills/gathering/mine.py`
- Modify: `anima/procedures/mine_ore.py`
- Modify: `anima/planner/planner.py`

- [ ] **Step 2.2.1: Add the breaker to Planner.__init__**

In `anima/planner/planner.py` `Planner.__init__`, add after the existing `self._last_escalation = 0.0`:

```python
        from anima.planner.circuit_breaker import CircuitBreaker
        # 1 failure in an 8×8 ore bank = the whole bank is empty for the
        # server's 10-20 min respawn window.
        self._bank_breaker = CircuitBreaker(max_failures=1, cooldown_s=600)
```

Also expose it on the blackboard so skills/procedures can reach it:

```python
# In Planner.run(), right after `self._running = True`:
        ctx.blackboard["_bank_breaker"] = self._bank_breaker
```

- [ ] **Step 2.2.2: Update `_find_mineable_tile` in mine.py**

In `anima/skills/gathering/mine.py`, replace the `_is_bank_depleted` helper and its call sites:

```python
    # Replace the depleted_banks dict lookup with a CircuitBreaker lookup.
    # Falls back to the old blackboard dict if the breaker isn't installed
    # (legacy callers / tests).
    breaker = ctx.blackboard.get("_bank_breaker")
    depleted_banks: dict[tuple[int, int], float] = ctx.blackboard.setdefault(
        "depleted_banks", {}
    )
    now = time.time()
    _blocked = blocked or set()

    def _is_bank_depleted(x: int, y: int) -> bool:
        key = _bank_key(x, y)
        if breaker is not None and breaker.is_open(key):
            return True
        ts = depleted_banks.get(key)
        if ts and now - ts < DEPLETED_COOLDOWN:
            return True
        if ts:
            del depleted_banks[key]
        return False
```

And in the `MineOre.execute()` failure branch (around line 365-375), add a breaker call next to the existing dict write:

```python
            if fails >= 3:
                depleted_banks: dict[tuple[int, int], float] = (
                    ctx.blackboard.setdefault("depleted_banks", {})
                )
                bk = _bank_key(tx, ty)
                depleted_banks[bk] = time.time()
                breaker = ctx.blackboard.get("_bank_breaker")
                if breaker is not None:
                    breaker.trip(bk)
                ctx.blackboard["_mine_consec_fail"] = 0
                logger.info(
                    "mine_bank_depleted",
                    pos=f"({tx},{ty})", bank=f"{bk}", fails=fails,
                )
```

- [ ] **Step 2.2.3: Update `mine_ore.py` procedure failures**

In `anima/procedures/mine_ore.py` replace each of the three depleted-banks write sites (depleted / too_far / los_fail branches) with a single helper. At the top of the file add:

```python
def _trip_bank(ctx, tx: int, ty: int) -> tuple[int, int]:
    """Mark the ore bank at (tx, ty) as depleted. Returns the bank key."""
    from anima.skills.gathering.mine import _bank_key
    bk = _bank_key(tx, ty)
    depleted_banks = ctx.blackboard.setdefault("depleted_banks", {})
    depleted_banks[bk] = __import__("time").time()
    breaker = ctx.blackboard.get("_bank_breaker")
    if breaker is not None:
        breaker.trip(bk)
    return bk
```

Then replace the three blocks (`_mine_flags["depleted"]`, `_mine_flags["too_far"]`, `_mine_flags["los_fail"]`) to call `_trip_bank(ctx, tx, ty)` instead of duplicating the dict/breaker logic.

- [ ] **Step 2.2.4: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 322+ tests PASS.

- [ ] **Step 2.2.5: Commit**

```bash
git add anima/planner/planner.py anima/skills/gathering/mine.py anima/procedures/mine_ore.py
git commit -m "Migrate mine bank depletion to CircuitBreaker (with dict fallback)"
```

## Task 2.3: Migrate craft_blacksmith material cooldown to CircuitBreaker

**Files:**
- Modify: `anima/procedures/craft_blacksmith.py`
- Modify: `anima/planner/planner.py`

- [ ] **Step 2.3.1: Add breaker to planner**

In `Planner.__init__`, add next to `_bank_breaker`:

```python
        # After 3 "insufficient metal" failures, cool down for 5 minutes.
        self._craft_material_breaker = CircuitBreaker(max_failures=3, cooldown_s=300)
```

In `Planner.run()` after setting `_bank_breaker` on the blackboard, also set:

```python
        ctx.blackboard["_craft_material_breaker"] = self._craft_material_breaker
```

- [ ] **Step 2.3.2: Replace ad-hoc material cooldown in craft_blacksmith**

In `anima/procedures/craft_blacksmith.py`:

Replace the `can_start` cooldown check:

```python
    async def can_start(self, ctx: AgentContext) -> bool:
        # CircuitBreaker replaces the old _craft_bs_material_cooldown flag.
        breaker = ctx.blackboard.get("_craft_material_breaker")
        if breaker is not None and breaker.is_open("iron"):
            return False
        # Legacy fallback for tests that don't install the breaker
        import time as _time
        if _time.time() < ctx.blackboard.get("_craft_bs_material_cooldown", 0):
            return False
        if not find_in_backpack(ctx, TONGS_GRAPHICS):
            return False
        if _count_iron_ingots(ctx) < MIN_INGOTS:
            return False
        return self._has_anvil_and_forge(ctx)
```

Replace the failure-tracking block in `execute()` (the "insufficient metal" branch):

```python
        if "sufficient metal" in notice_lower or "sufficient material" in notice_lower:
            # Track through the circuit breaker instead of blackboard counters.
            breaker = ctx.blackboard.get("_craft_material_breaker")
            if breaker is not None and ingots_before >= ingot_cost:
                breaker.record_failure("iron")
                if breaker.is_open("iron"):
                    logger.warning(
                        "craft_bs_material_cooldown",
                        fails=breaker.failure_count("iron"),
                        ingots=ingots_before,
                        cost=ingot_cost,
                    )
            # Keep legacy counter for tests that don't install the breaker
            mat_fails = ctx.blackboard.get("_craft_bs_material_fails", 0) + 1
            ctx.blackboard["_craft_bs_material_fails"] = mat_fails
            if mat_fails >= 3 and ingots_before >= ingot_cost:
                ctx.blackboard["_craft_bs_material_cooldown"] = time.time() + 300
```

On craft success, add a breaker reset:

```python
            ctx.blackboard["_craft_bs_fails"] = 0
            ctx.blackboard["_craft_bs_material_fails"] = 0
            breaker = ctx.blackboard.get("_craft_material_breaker")
            if breaker is not None:
                breaker.record_success("iron")
```

- [ ] **Step 2.3.3: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 322+ tests PASS.

- [ ] **Step 2.3.4: Commit**

```bash
git add anima/planner/planner.py anima/procedures/craft_blacksmith.py
git commit -m "Migrate craft_blacksmith material cooldown to CircuitBreaker"
```

## Task 2.4: Migrate ore-pickup failures to CircuitBreaker

**Files:**
- Modify: `anima/planner/planner.py` (`Planner.__init__` and `_PickUpAndSmelt.run`)

- [ ] **Step 2.4.1: Add breaker to planner**

In `Planner.__init__`:

```python
        # 2 server-refused pickups on the same ore serial = mark it junk.
        self._ore_pickup_breaker = CircuitBreaker(max_failures=2, cooldown_s=3600)
```

In `Planner.run()`:

```python
        ctx.blackboard["_ore_pickup_breaker"] = self._ore_pickup_breaker
```

- [ ] **Step 2.4.2: Update `_PickUpAndSmelt.run()`**

Replace the existing per-serial `fail_counts` dict logic with breaker calls. Find the pickup failure block and replace:

```python
            elif ore_check and ore_check.container == 0:
                # Server refused the pickup — record in the breaker and
                # add to the junk set once the breaker opens.
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_failure(ore_item.serial)
                    opened = breaker.is_open(ore_item.serial)
                else:
                    # Fallback to legacy counter
                    fails = fail_counts.get(ore_item.serial, 0) + 1
                    fail_counts[ore_item.serial] = fails
                    opened = fails >= 2
                logger.warning(
                    "ore_pickup_failed",
                    serial=f"0x{ore_item.serial:08X}",
                    reason="still on ground after pick_up",
                )
                if opened:
                    junk.add(ore_item.serial)
                    hard_failures.append(ore_item.serial)
                    if breaker is not None:
                        breaker.reset(ore_item.serial)
                    else:
                        fail_counts.pop(ore_item.serial, None)
                    logger.info(
                        "ore_marked_junk",
                        serial=f"0x{ore_item.serial:08X}",
                        reason="repeated pickup failure",
                    )
```

On successful pickup, also reset the breaker:

```python
            if ore_check and ore_check.container == backpack:
                picked += 1
                fail_counts.pop(ore_item.serial, None)
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_success(ore_item.serial)
                logger.info("picked_up_ore", ...)
```

- [ ] **Step 2.4.3: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 322+ tests PASS.

- [ ] **Step 2.4.4: Commit**

```bash
git add anima/planner/planner.py
git commit -m "Migrate ore-pickup failure tracking to CircuitBreaker"
```

---

# Phase 3: Fix Lock (prevent duplicate Claude Code sessions)

**Goal:** When supervisor detects a problem and calls Claude Code, any parallel human or Claude Code instance should see "this problem is already being worked on" and skip.

## Task 3.1: Create fix lock helper

**Files:**
- Create: `tools/fix_lock.py`
- Create: `tests/test_fix_lock.py`

- [ ] **Step 3.1.1: Write failing tests**

Create `tests/test_fix_lock.py`:

```python
"""Tests for the fix lock helper."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.fix_lock import (
    FixLock, acquire_fix_lock, release_fix_lock, is_fix_locked,
)


@pytest.fixture
def tmp_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "fixing.json"
    monkeypatch.setattr("tools.fix_lock.LOCK_FILE", lock_path)
    return lock_path


class TestFixLock:
    def test_acquire_creates_file(self, tmp_lock):
        ok = acquire_fix_lock("procedure_spam:mine_ore", pid=12345, sha="abc1234")
        assert ok is True
        assert tmp_lock.exists()
        data = json.loads(tmp_lock.read_text())
        assert data["problem"] == "procedure_spam:mine_ore"
        assert data["pid"] == 12345
        assert data["sha"] == "abc1234"

    def test_acquire_conflicts_on_same_problem(self, tmp_lock):
        assert acquire_fix_lock("X", pid=1, sha="abc") is True
        assert acquire_fix_lock("X", pid=2, sha="abc") is False

    def test_acquire_succeeds_after_sha_changed(self, tmp_lock):
        # First session fixed the problem at abc → new session at def should
        # be allowed even if the lock was never released.
        assert acquire_fix_lock("X", pid=1, sha="abc") is True
        assert acquire_fix_lock("X", pid=2, sha="def") is True

    def test_release_removes_file(self, tmp_lock):
        acquire_fix_lock("X", pid=1, sha="abc")
        release_fix_lock()
        assert not tmp_lock.exists()

    def test_is_fix_locked(self, tmp_lock):
        assert is_fix_locked("X") is False
        acquire_fix_lock("X", pid=1, sha="abc")
        assert is_fix_locked("X") is True
        assert is_fix_locked("Y") is False  # different problem

    def test_stale_lock_expires(self, tmp_lock):
        """A lock older than MAX_LOCK_AGE is ignored."""
        from tools.fix_lock import MAX_LOCK_AGE
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 999999, "sha": "abc",
            "started": time.time() - MAX_LOCK_AGE - 10,
        }))
        assert is_fix_locked("X") is False
        # New lock should acquire successfully
        assert acquire_fix_lock("X", pid=1, sha="abc") is True

    def test_missing_file_not_locked(self, tmp_lock):
        assert is_fix_locked("anything") is False

    def test_malformed_file_not_locked(self, tmp_lock):
        tmp_lock.write_text("not json")
        assert is_fix_locked("X") is False

    def test_context_manager(self, tmp_lock):
        with FixLock("X", pid=1, sha="abc") as ok:
            assert ok is True
            assert is_fix_locked("X") is True
        assert is_fix_locked("X") is False

    def test_context_manager_conflict(self, tmp_lock):
        acquire_fix_lock("X", pid=1, sha="abc")
        with FixLock("X", pid=2, sha="abc") as ok:
            assert ok is False
        # Outer lock still held
        assert is_fix_locked("X") is True
```

- [ ] **Step 3.1.2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_fix_lock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.fix_lock'`

- [ ] **Step 3.1.3: Implement fix_lock.py**

Create `tools/fix_lock.py`:

```python
"""File-based lock to prevent duplicate Claude Code fix sessions.

When the supervisor launches a Claude Code session to investigate a
problem, it writes a lock file naming the problem, the session PID, and
the git SHA it was working from. A second process (human or another
supervisor instance) checking the lock will see the problem is already
being worked on and skip.

The lock is automatically ignored if:
  - the git SHA has moved on (the problem was already fixed)
  - the lock is older than MAX_LOCK_AGE (the session crashed)
  - the file is missing or malformed
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
LOCK_FILE = ROOT / "data" / "fixing.json"

# Lock is considered stale after this many seconds (covers crashed sessions)
MAX_LOCK_AGE = 1800  # 30 minutes


def _read_lock() -> dict | None:
    try:
        return json.loads(LOCK_FILE.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _current_sha() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def is_fix_locked(problem: str) -> bool:
    """True if another session is actively fixing the given problem."""
    data = _read_lock()
    if not data:
        return False
    if data.get("problem") != problem:
        return False
    # Stale?
    started = data.get("started", 0)
    if time.time() - started > MAX_LOCK_AGE:
        return False
    # SHA moved on?
    locked_sha = data.get("sha", "")
    current = _current_sha()
    if locked_sha and current and locked_sha != current:
        return False
    return True


def acquire_fix_lock(problem: str, pid: int, sha: str) -> bool:
    """Try to acquire the fix lock for `problem`. Returns False on conflict."""
    if is_fix_locked(problem):
        return False
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "problem": problem,
        "pid": pid,
        "sha": sha,
        "started": time.time(),
    }, indent=2))
    return True


def release_fix_lock() -> None:
    """Remove the lock file (ignored if it doesn't exist)."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


class FixLock:
    """Context manager for acquire/release.

    Usage:
        with FixLock("procedure_spam:mine_ore", pid=os.getpid(), sha="abc") as ok:
            if ok:
                run_claude_code()
    """

    def __init__(self, problem: str, pid: Optional[int] = None, sha: str = "") -> None:
        self._problem = problem
        self._pid = pid if pid is not None else os.getpid()
        self._sha = sha or _current_sha()
        self._owned = False

    def __enter__(self) -> bool:
        self._owned = acquire_fix_lock(self._problem, self._pid, self._sha)
        return self._owned

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned:
            release_fix_lock()
```

- [ ] **Step 3.1.4: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_fix_lock.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 3.1.5: Commit**

```bash
git add tools/fix_lock.py tests/test_fix_lock.py
git commit -m "Add fix lock: prevent duplicate Claude Code fix sessions"
```

## Task 3.2: Supervisor honors fix lock

**Files:**
- Modify: `tools/supervisor.py`

- [ ] **Step 3.2.1: Wrap targeted fix in FixLock**

In `tools/supervisor.py`, at the top add:

```python
from fix_lock import FixLock  # tools is on sys.path
```

In the main loop's Level 2 (targeted fix) branch, wrap the Claude call. Find this line:

```python
                            head_before = get_git_head()
                            success, output = call_claude_with_prompt(prompt, timeout=timeout)
```

Replace with:

```python
                            head_before = get_git_head()
                            lock_key = f"targeted_fix:{fix_key}"
                            with FixLock(lock_key, sha=head_before) as got:
                                if not got:
                                    print(f"[supervisor] {lock_key} already being fixed — skipping")
                                    continue
                                success, output = call_claude_with_prompt(prompt, timeout=timeout)
```

- [ ] **Step 3.2.2: Wrap full analysis in FixLock**

In the Level 3 full analysis block, find:

```python
                        prompt = build_diagnostic_prompt(minutes=args.minutes)
                        head_before = get_git_head()
                        _, output = call_claude_with_prompt(prompt, timeout=900)
```

Replace with:

```python
                        prompt = build_diagnostic_prompt(minutes=args.minutes)
                        head_before = get_git_head()
                        lock_key = f"full_analysis:{severe[0]['name']}"
                        with FixLock(lock_key, sha=head_before) as got:
                            if not got:
                                print(f"[supervisor] {lock_key} already being fixed — skipping")
                                agent_proc = start_agent(args.agent_args)
                                last_analysis = time.time()
                                continue
                            _, output = call_claude_with_prompt(prompt, timeout=900)
```

- [ ] **Step 3.2.3: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 332+ tests PASS.

- [ ] **Step 3.2.4: Commit**

```bash
git add tools/supervisor.py
git commit -m "Supervisor: honor fix lock to avoid duplicate Claude Code sessions"
```

---

# Phase 4: Planner Split

**Goal:** Shrink `planner.py` from 1841 lines so Claude Code can analyze it faster during self-improvement cycles, and so individual subsystems are unit-testable in isolation.

This phase is more invasive — do it only after phases 1-3 have stabilized and all tests are green.

## Task 4.1: Extract procedure helper classes

**Files:**
- Create: `anima/planner/helpers.py`
- Modify: `anima/planner/planner.py`

- [ ] **Step 4.1.1: Create helpers.py**

Create `anima/planner/helpers.py` with the three direct-instantiation procedure classes currently embedded in `planner.py`: `_MoveToProcedure`, `_PickUpAndSmelt`, `_ScavengeGroundItems`. Copy the full class definitions from `planner.py` (search for `class _MoveToProcedure`, `class _PickUpAndSmelt`, `class _ScavengeGroundItems`).

Top of file:

```python
"""Helper procedures instantiated directly by the planner.

These are not registered with the ProcedureRegistry — the planner
constructs them inline when it needs ad-hoc behavior (move to a
location, pick up and smelt a specific ore pile, scavenge for deadlock
recovery). Their `run()` methods are the interface the planner calls.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


# ... (paste the three class definitions here) ...
```

- [ ] **Step 4.1.2: Update planner.py imports**

In `anima/planner/planner.py`, delete the three class definitions and add at the top:

```python
from anima.planner.helpers import (
    _MoveToProcedure,
    _PickUpAndSmelt,
    _ScavengeGroundItems,
)
```

- [ ] **Step 4.1.3: Run all tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 332+ tests PASS (file count is now lower in planner.py).

- [ ] **Step 4.1.4: Commit**

```bash
git add anima/planner/helpers.py anima/planner/planner.py
git commit -m "Extract _MoveToProcedure/_PickUpAndSmelt/_ScavengeGroundItems to helpers.py"
```

## Task 4.2: Extract deadlock / escalation logic

**Files:**
- Create: `anima/planner/deadlock.py`
- Modify: `anima/planner/planner.py`

- [ ] **Step 4.2.1: Create deadlock.py**

Create `anima/planner/deadlock.py` and move `_resolve_deadlock`, `_escalate_to_forum`, `_compose_help_post`, and `_find_ground_valuables` into it as module-level functions (or as a `DeadlockResolver` class taking `planner` in the constructor).

Recommended: take the `Planner` instance as a constructor argument so the resolver can read/write private planner state without changing it into public attrs.

```python
"""Deadlock resolution and forum escalation — extracted from planner.py."""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.planner.planner import Planner

logger = structlog.get_logger()


class DeadlockResolver:
    def __init__(self, planner: "Planner") -> None:
        self._planner = planner

    async def resolve(self, ctx: "AgentContext") -> None:
        """Run the deadlock recovery strategies in order."""
        # ... (paste _resolve_deadlock body) ...

    async def escalate_to_forum(self, ctx: "AgentContext") -> None:
        # ... (paste _escalate_to_forum body) ...

    async def _compose_help_post(
        self, ctx: "AgentContext", persona_name: str,
        situation: str, has_pickaxe: bool,
    ) -> str:
        # ... (paste _compose_help_post body) ...
```

- [ ] **Step 4.2.2: Update planner.py**

In `anima/planner/planner.py`:
- Delete `_resolve_deadlock`, `_escalate_to_forum`, `_compose_help_post` methods
- In `Planner.__init__`, add: `self._deadlock = DeadlockResolver(self)`
- Replace calls `await self._resolve_deadlock(ctx)` with `await self._deadlock.resolve(ctx)`
- Replace calls `await self._escalate_to_forum(ctx)` with `await self._deadlock.escalate_to_forum(ctx)`

- [ ] **Step 4.2.3: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 332+ tests PASS.

- [ ] **Step 4.2.4: Commit**

```bash
git add anima/planner/deadlock.py anima/planner/planner.py
git commit -m "Extract deadlock resolution and forum escalation to deadlock.py"
```

## Task 4.3: Extract roaming / movement logic

**Files:**
- Create: `anima/planner/roaming.py`
- Modify: `anima/planner/planner.py`

- [ ] **Step 4.3.1: Create roaming.py**

Move `_move_to_location`, `_try_move_to_activity`, `_mark_nearby_mine_exhausted`, `_find_waypoint_toward`, `_is_destination_failed` to `anima/planner/roaming.py` as a `RoamingHelper` class constructed with a Planner reference.

- [ ] **Step 4.3.2: Update planner.py**

In `Planner.__init__`:

```python
        from anima.planner.roaming import RoamingHelper
        self._roaming = RoamingHelper(self)
```

Replace calls:
- `await self._move_to_location(ctx, ...)` → `await self._roaming.move_to_location(ctx, ...)`
- `await self._try_move_to_activity(ctx)` → `await self._roaming.try_move_to_activity(ctx)`
- `self._mark_nearby_mine_exhausted(ctx, ss)` → `self._roaming.mark_nearby_mine_exhausted(ctx, ss)`
- `self._is_destination_failed(x, y)` → `self._roaming.is_destination_failed(x, y)`

- [ ] **Step 4.3.3: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/ -q`
Expected: 332+ tests PASS.

- [ ] **Step 4.3.4: Commit**

```bash
git add anima/planner/roaming.py anima/planner/planner.py
git commit -m "Extract location roaming helpers to roaming.py"
```

- [ ] **Step 4.3.5: Check file sizes**

Run: `wc -l anima/planner/*.py`
Expected: `planner.py` is now below 1200 lines, and `helpers.py`, `deadlock.py`, `roaming.py` are each under 500 lines.

---

# Phase 5: Typed Blackboard (optional)

**Goal:** Migrate untyped blackboard dict access to a typed dataclass so stale-key bugs (typos) are caught statically and the state surface is documented in one place.

This phase is large and optional — do it last, and only if phases 1-4 are stable.

## Task 5.1: Create PlannerBlackboard dataclass

**Files:**
- Create: `anima/planner/state.py`
- Create: `tests/test_planner_state.py`

- [ ] **Step 5.1.1: Write failing tests**

Create `tests/test_planner_state.py`:

```python
"""Tests for the typed PlannerBlackboard wrapper."""
from anima.planner.state import PlannerBlackboard


class TestPlannerBlackboard:
    def test_default_construction(self):
        bb = PlannerBlackboard()
        assert bb.craft_bs_fails == 0
        assert bb.skip_procedures == set()
        assert bb.depleted_banks == {}
        assert bb.junk_ore_serials == set()

    def test_from_dict_roundtrip(self):
        """Loading from and writing to a dict preserves all fields."""
        data = {
            "_craft_bs_fails": 5,
            "_skip_procedures": {"mine_ore"},
            "depleted_banks": {(10, 20): 1234.5},
            "_junk_ore_serials": {0x4001},
        }
        bb = PlannerBlackboard.from_dict(data)
        assert bb.craft_bs_fails == 5
        assert bb.skip_procedures == {"mine_ore"}
        assert bb.depleted_banks == {(10, 20): 1234.5}
        assert bb.junk_ore_serials == {0x4001}

        out = bb.to_dict()
        assert out["_craft_bs_fails"] == 5
        assert out["_skip_procedures"] == {"mine_ore"}

    def test_from_dict_unknown_keys_preserved(self):
        """Unknown keys are kept in extras so we don't lose legacy state."""
        data = {"legacy_flag": True}
        bb = PlannerBlackboard.from_dict(data)
        assert bb.extras["legacy_flag"] is True
        out = bb.to_dict()
        assert out["legacy_flag"] is True
```

- [ ] **Step 5.1.2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner_state.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 5.1.3: Implement state.py**

Create `anima/planner/state.py`:

```python
"""Typed blackboard state for the planner.

Replaces the untyped dict-based state scattered as dozens of string keys.
Unknown keys are preserved in `extras` so legacy code that still uses
dict access doesn't break during migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlannerBlackboard:
    # --- Procedure failure / skip state ---
    craft_bs_fails: int = 0
    craft_bs_material_fails: int = 0
    craft_bs_material_cooldown: float = 0.0
    make_tools_gave_up: bool = False
    skip_procedures: set[str] = field(default_factory=set)

    # --- Mining state ---
    depleted_banks: dict[tuple[int, int], float] = field(default_factory=dict)
    depleted_mines: dict[tuple[int, int], float] = field(default_factory=dict)  # legacy
    exhausted_mines: dict[str, float] = field(default_factory=dict)
    mine_exhausted_until: float = 0.0
    mine_consec_fail: int = 0
    junk_ore_serials: set[int] = field(default_factory=set)
    unsmelable_ore_hues: set[int] = field(default_factory=set)
    ore_pickup_fails: dict[int, int] = field(default_factory=dict)

    # --- Vendor / trade state ---
    refused_vendors: dict[int, float] = field(default_factory=dict)
    failed_destinations: dict[tuple[int, int], float] = field(default_factory=dict)

    # --- Intent / UI ---
    planner_intent: str = ""
    current_procedure: str | None = None

    # --- Anything else (legacy keys) ---
    extras: dict[str, Any] = field(default_factory=dict)

    # --- Field name mapping: attribute name -> blackboard key ---
    _KEY_MAP = {
        "craft_bs_fails": "_craft_bs_fails",
        "craft_bs_material_fails": "_craft_bs_material_fails",
        "craft_bs_material_cooldown": "_craft_bs_material_cooldown",
        "make_tools_gave_up": "_make_tools_gave_up",
        "skip_procedures": "_skip_procedures",
        "depleted_banks": "depleted_banks",
        "depleted_mines": "depleted_mines",
        "exhausted_mines": "exhausted_mines",
        "mine_exhausted_until": "_mine_exhausted_until",
        "mine_consec_fail": "_mine_consec_fail",
        "junk_ore_serials": "_junk_ore_serials",
        "unsmelable_ore_hues": "_unsmelable_ore_hues",
        "ore_pickup_fails": "_ore_pickup_fails",
        "refused_vendors": "refused_vendors",
        "failed_destinations": "_failed_destinations",
        "planner_intent": "planner_intent",
        "current_procedure": "current_procedure",
    }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannerBlackboard":
        kwargs: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        known_keys = set(cls._KEY_MAP.values())
        for attr, key in cls._KEY_MAP.items():
            if key in data:
                kwargs[attr] = data[key]
        for k, v in data.items():
            if k not in known_keys:
                extras[k] = v
        kwargs["extras"] = extras
        return cls(**kwargs)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for attr, key in self._KEY_MAP.items():
            val = getattr(self, attr)
            # Don't write default-empty containers (reduces noise in
            # serialized state) but do write scalars.
            if isinstance(val, (dict, set, list)) and not val:
                continue
            if isinstance(val, (int, float)) and val == 0:
                continue
            if val is None or val == "":
                continue
            out[key] = val
        out.update(self.extras)
        return out
```

- [ ] **Step 5.1.4: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner_state.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5.1.5: Commit**

```bash
git add anima/planner/state.py tests/test_planner_state.py
git commit -m "Add PlannerBlackboard: typed wrapper for planner state"
```

## Task 5.2: Document the migration boundary

**Files:**
- Modify: `anima/planner/planner.py` (add docstring comment)

- [ ] **Step 5.2.1: Add migration note**

Add a comment at the top of `anima/planner/planner.py` just below the module docstring:

```python
# NOTE: `ctx.blackboard` is in the process of migrating to the typed
# PlannerBlackboard in anima/planner/state.py. New code should prefer
# `PlannerBlackboard.from_dict(ctx.blackboard)` and write back via
# `bb.to_dict()` at the end of the operation. Existing string-key access
# is still supported during the migration.
```

- [ ] **Step 5.2.2: Commit**

```bash
git add anima/planner/planner.py
git commit -m "Document PlannerBlackboard migration path in planner.py"
```

---

# Completion Criteria

- All 35+ tests added across phases pass (target: 347+ total tests)
- `planner.py` line count drops from 1841 to under 1200 after phase 4
- No regressions in existing behavior (manual smoke test with a running agent)
- All new modules have docstrings explaining their responsibility
- No new `_xyz_cooldown` or `_xyz_fails` blackboard keys introduced — all new cooldown logic uses `CircuitBreaker`

## Phase dependencies

```
Phase 1 (health) ─────┐
                      ├─→ Phase 2 (circuit breakers)
                      │     ↓
                      ├─→ Phase 3 (fix lock)  [can run in parallel with 2]
                      │     ↓
                      └─→ Phase 4 (planner split)
                            ↓
                          Phase 5 (typed blackboard) [optional]
```

Phase 2 and Phase 3 touch different files and can be worked on in parallel by separate subagents.

## Out of scope

These were discussed but are deliberately NOT in this plan:

- **Full rewrite of the priority system into a decision table** — too risky without production metrics
- **Replay/simulation mode for debugging** — useful but big, defer
- **Dashboard / live monitoring UI** — nice-to-have, separate project
- **Graceful degradation on server quirks** — covered incrementally by existing loop fixes
