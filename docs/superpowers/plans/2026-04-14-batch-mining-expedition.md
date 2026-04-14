# Batch Mining Expedition — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the mine→smelt→craft→sell loop around explicit session phases so the agent mines out several banks before going to forge, collects all ground piles in a single tour, and only crafts weapons when the cycle is done — per `docs/superpowers/specs/2026-04-14-batch-mining-design.md`.

**Architecture:** A new `MiningExpedition` class on the `Planner` owns a `Phase` (MINING | COLLECTING | CRAFTING_TRIP | IDLE), a list of `PileRecord`s, and phase-transition predicates. The planner's Priority 3/3b (batch smelt / ground-ore pickup) and Priority 5 (batch craft) branches become phase-gated. `mine.py` calls `note_ore_mined()` on each success to register piles; `_PickUpAndSmelt` prefers remembered pile positions over perception-based scanning.

**Tech Stack:** Python 3.12, `dataclasses`, `enum`, `pytest`, `pytest-asyncio`, `structlog`.

---

## File Structure

**Create:**
- `anima/planner/expedition.py` — `Phase` enum, `PileRecord`, `MiningExpedition` class
- `tests/test_expedition.py` — unit tests for the class

**Modify:**
- `anima/planner/planner.py` — wire expedition into `Planner.__init__`, publish to blackboard each tick, phase-gate Priority 3/3b/5, drive transitions
- `anima/skills/gathering/mine.py` — call `note_ore_mined()` on successful mine
- `anima/planner/helpers.py` — `_PickUpAndSmelt` accepts/reads expedition, prefers remembered piles, marks them collected

**Not touched:** `CircuitBreaker`, `craft_blacksmith`, `sell_to_vendor`, `buy_from_vendor`, `make_tools`, `RoamingHelper`, `DeadlockResolver`, `StrategySelector`.

---

## Task 1: `MiningExpedition` class with data model and mutators

**Files:**
- Create: `anima/planner/expedition.py`
- Test: `tests/test_expedition.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_expedition.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_expedition.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anima.planner.expedition'`

- [ ] **Step 3: Implement `MiningExpedition`**

Create `anima/planner/expedition.py`:

```python
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
        one. Sets home_base on first call. Transitions IDLE → MINING.
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
        """Drop piles older than decay_s seconds."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_expedition.py -v`
Expected: PASS (all 10 tests)

- [ ] **Step 5: Commit**

```bash
git add anima/planner/expedition.py tests/test_expedition.py
git commit -m "Add MiningExpedition state model

Phase enum, PileRecord, and MiningExpedition dataclass with
note_ore_mined / mark_pile_collected / transition_to / prune_stale_piles.
Pure data + logging; planner wiring comes next."
```

---

## Task 2: Phase-transition predicates

**Files:**
- Modify: `anima/planner/expedition.py`
- Modify: `tests/test_expedition.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_expedition.py`:

```python
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

    def test_should_start_collecting_false_when_scan_nonempty(self):
        exp = MiningExpedition()
        exp.transition_to(Phase.MINING)
        exp.note_ore_mined(x=100, y=100, bank_key=(12, 12))
        assert exp.should_start_collecting(scan_empty=False) is False

    def test_should_start_collecting_false_outside_mining(self):
        exp = MiningExpedition()  # IDLE
        exp.piles.append(PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time()))
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
            ingot_count=8, crafted_count=0, near_home=True,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_expedition.py::TestPhaseTransitionPredicates -v`
Expected: FAIL with `AttributeError: 'MiningExpedition' object has no attribute 'should_start_collecting'`

- [ ] **Step 3: Implement predicates**

Append to `anima/planner/expedition.py` inside the `MiningExpedition` class:

```python
    # --- Phase-transition predicates ---

    def should_start_collecting(self, scan_empty: bool) -> bool:
        """MINING → COLLECTING when scan returned no banks and piles exist."""
        return (
            self.phase == Phase.MINING
            and scan_empty
            and len(self.piles) > 0
        )

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
        """True if the current phase has been active too long without progress."""
        return time.time() - self.phase_started_at > max_phase_s
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_expedition.py -v`
Expected: PASS (all ~27 tests)

- [ ] **Step 5: Commit**

```bash
git add anima/planner/expedition.py tests/test_expedition.py
git commit -m "Add phase-transition predicates to MiningExpedition

should_start_collecting, should_leave_mine, should_return_to_mine,
watchdog_expired. All take primitive args for testability; planner
computes the inputs once per tick."
```

---

## Task 3: Wire expedition into `Planner` and publish to blackboard

**Files:**
- Modify: `anima/planner/planner.py`
- Modify: `tests/test_planner.py` (add one test)

- [ ] **Step 1: Write the failing test**

Prerequisite: ensure `tests/test_planner.py` has `import time` at the top (it currently imports `import time as _time` deep in the file around line 201 — add a plain `import time` near the other top-level imports so the new test classes below can call `time.time()` directly).

Add to `tests/test_planner.py` at the end of the file:

```python
class TestExpeditionWiring:
    @pytest.mark.asyncio
    async def test_expedition_attached_to_planner(self):
        """Planner.__init__ creates a MiningExpedition."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        assert isinstance(planner._expedition, MiningExpedition)

    @pytest.mark.asyncio
    async def test_expedition_published_to_blackboard(self):
        """After _select_procedure, ctx.blackboard['expedition'] is set."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        ctx = _make_ctx()
        await planner._select_procedure(ctx)
        assert isinstance(ctx.blackboard.get("expedition"), MiningExpedition)
        assert ctx.blackboard["expedition"] is planner._expedition
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestExpeditionWiring -v`
Expected: FAIL — `AttributeError: 'Planner' object has no attribute '_expedition'`

- [ ] **Step 3: Wire the expedition into the planner**

In `anima/planner/planner.py`, add an import near the other planner imports:

```python
from anima.planner.expedition import MiningExpedition, Phase
```

In `Planner.__init__` (near the other `self._*` helper assignments around line 115–118), add:

```python
        self._expedition = MiningExpedition()
```

In the method that the current code calls for each tick's procedure selection (find it by searching for `async def _select_procedure` — it's the method that houses the Priority comments), publish the expedition to the blackboard as the very first action inside the method body:

```python
        ctx.blackboard["expedition"] = self._expedition
        # Prune piles that have likely decayed server-side.
        self._expedition.prune_stale_piles()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestExpeditionWiring -v`
Expected: PASS

Run the full planner test suite to confirm nothing regressed:

Run: `uv run pytest tests/test_planner.py -v`
Expected: PASS (all existing tests still pass)

- [ ] **Step 5: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Wire MiningExpedition into Planner

Create the instance in __init__ and publish to blackboard each tick.
No behavior change yet — Priority 3/3b/5 still use the old logic."
```

---

## Task 4: Hook `note_ore_mined` into `MineOre` procedure

**Context:** The codebase has two MineOre implementations:
- `anima/procedures/mine_ore.py::MineOre` — the v2 Procedure used by the planner (live agent).
- `anima/skills/gathering/mine.py::MineOre` — the legacy v1 Skill (used in `--legacy` mode).

Only the Procedure version is hooked here; the Skill version is unchanged. `_find_mineable_tile` and `_bank_key` live in `anima/skills/gathering/mine.py` and are imported by both versions, so the hook uses the shared helper.

**Files:**
- Modify: `anima/procedures/mine_ore.py`
- Test: `tests/test_procedure_mine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_procedure_mine.py`:

```python
class TestMineOreExpeditionHook:
    @pytest.mark.asyncio
    async def test_successful_mine_registers_pile(self):
        """A successful mine adds a pile and transitions IDLE → MINING."""
        from anima.planner.expedition import MiningExpedition, Phase
        from anima.procedures.mine_ore import MineOre
        from anima.skills.gathering.mine import _bank_key

        proc = MineOre()
        exp = MiningExpedition()
        ctx = _make_ctx()
        ctx.blackboard = {"expedition": exp}

        # Pickaxe in backpack
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        # Ore created after mining — container == backpack, graphic is iron ore
        ore = MagicMock(container=0x101, graphic=0x19B9, amount=1, serial=0x300, hue=0)
        ctx.perception.world.items = {0x200: pickaxe, 0x300: ore}

        # Bypass the server round-trip: mine succeeds with one ore gained.
        with patch(
            "anima.procedures.mine_ore._find_mineable_tile",
            return_value=(2500, 550, 15, 220, False),
        ), patch(
            "anima.procedures.mine_ore.use_on_target",
            new=AsyncMock(return_value=MagicMock(success=True, message="ok")),
        ), patch(
            "anima.procedures.mine_ore.asyncio.sleep",
            new=AsyncMock(),
        ):
            # Inflate ore_after so `ore_gained > 0` branch runs.
            # The procedure counts ore in backpack after the server response;
            # we simply add extra amount by mutating the existing item.
            ore.amount = 2  # matches "ore_after = 2, ore_before = 1"
            ctx.bus = None

            # ore_before is calculated once at the top, so we pre-set amount
            # such that before=1 and after=2. Simulate this by tweaking amount
            # just before `use_on_target` finishes — easier: monkeypatch the
            # second count. Inline: use a stateful counter.
            call_count = [0]
            original_items = dict(ctx.perception.world.items)

            def _items_property():
                call_count[0] += 1
                if call_count[0] == 1:
                    # ore_before count: pretend amount=1
                    return {0x200: pickaxe, 0x300: MagicMock(
                        container=0x101, graphic=0x19B9, amount=1, serial=0x300, hue=0,
                    )}
                else:
                    # ore_after count: amount=2
                    return original_items

            type(ctx.perception.world).items = property(
                lambda self: _items_property()
            )

            result = await proc.execute(ctx)

        assert result.success is True
        assert len(exp.piles) == 1
        assert exp.piles[0].x == ctx.perception.self_state.x
        assert exp.piles[0].y == ctx.perception.self_state.y
        assert exp.piles[0].bank_key == _bank_key(
            ctx.perception.self_state.x, ctx.perception.self_state.y,
        )
        assert exp.phase == Phase.MINING

    @pytest.mark.asyncio
    async def test_mine_without_expedition_does_not_crash(self):
        """With no 'expedition' key on the blackboard, mining must still work."""
        from anima.procedures.mine_ore import MineOre

        proc = MineOre()
        ctx = _make_ctx()
        ctx.blackboard = {}

        # Rig the procedure to fail early (no tools) — exercises the path
        # without needing full mine plumbing; verifies no AttributeError
        # from a missing expedition key.
        result = await proc.execute(ctx)
        assert result.success is False
        # If the hook tried to call .note_ore_mined() on None, this would have
        # raised AttributeError instead.
```

*If the stateful `_items_property` approach proves brittle in the existing harness, simplify the first test by calling `exp.note_ore_mined(...)` manually after a successful `proc.execute` and asserting the hook fires at all. The critical invariant is that `execute()` invokes the hook in the `ore_gained > 0` path.*

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_procedure_mine.py::TestMineOreExpeditionHook -v`
Expected: FAIL — the hook doesn't exist yet.

- [ ] **Step 3: Add the hook in `anima/procedures/mine_ore.py`**

In the `ore_gained > 0` branch of `MineOre.execute` (around line 186–232 of `mine_ore.py`), immediately before the `return ProcedureResult(success=True, ...)` statement, add:

```python
            expedition = ctx.blackboard.get("expedition")
            if expedition is not None:
                from anima.skills.gathering.mine import _bank_key
                expedition.note_ore_mined(ss.x, ss.y, _bank_key(ss.x, ss.y))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_procedure_mine.py -v`
Expected: PASS (including existing tests)

- [ ] **Step 5: Commit**

```bash
git add anima/procedures/mine_ore.py tests/test_procedure_mine.py
git commit -m "Register ore pile with expedition on successful mine

anima/procedures/mine_ore.py::MineOre.execute now calls
expedition.note_ore_mined() on the success path when the planner has
published an expedition to the blackboard. No effect when absent,
keeping the procedure compatible with older test harnesses."
```

---

## Task 5: Update `_PickUpAndSmelt` to prefer remembered piles

**Files:**
- Modify: `anima/planner/helpers.py`
- Test: `tests/test_planner.py` (add a class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`:

```python
class TestPickUpAndSmeltWithExpedition:
    @pytest.mark.asyncio
    async def test_uses_pile_location_when_available(self):
        """When expedition has piles, _PickUpAndSmelt targets the nearest pile."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        exp.piles = [
            PileRecord(x=2460, y=558, bank_key=(307, 69), est_amount=3, last_seen_ts=time.time()),
            PileRecord(x=2480, y=560, bank_key=(310, 70), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        ctx.perception.self_state.x = 2465
        ctx.perception.self_state.y = 559
        ctx.blackboard["expedition"] = exp

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[], ss=ctx.perception.self_state)
            # The nearest pile is (2460, 558) at distance 5 (vs. distance ~15)
            # Run the procedure (it will go_to that pile and attempt pickup).
            await proc.run(ctx)
            # go_to should have been called for (2460, 558)
            import anima.action.movement
            # We patched it — check the patched AsyncMock was awaited with the nearest pile
            assert anima.action.movement.go_to.await_args.args[1:3] == (2460, 558)

    @pytest.mark.asyncio
    async def test_marks_pile_collected_on_success(self):
        """After picking up, the pile is removed from expedition memory."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        pile = PileRecord(x=2460, y=558, bank_key=(307, 69), est_amount=1, last_seen_ts=time.time())
        exp.piles = [pile]

        ctx = _make_ctx()
        ctx.perception.self_state.x = 2460
        ctx.perception.self_state.y = 558
        ctx.blackboard["expedition"] = exp

        # Put one ore on the ground at the pile position
        ore = MagicMock(
            serial=0xDEAD0001, graphic=0x19B9, amount=2, container=0,
            x=2460, y=558, z=0,
        )
        ctx.perception.world.items = {ore.serial: ore}

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[ore], ss=ctx.perception.self_state)
            result = await proc.run(ctx)

        assert result.success is True
        assert pile not in exp.piles

    @pytest.mark.asyncio
    async def test_removes_pile_on_go_to_failure(self):
        """If we can't reach the pile, remove it from memory."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        pile = PileRecord(x=9999, y=9999, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        exp.piles = [pile]

        ctx = _make_ctx()
        ctx.blackboard["expedition"] = exp

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=False)):
            proc = _PickUpAndSmelt(ground_ore=[], ss=ctx.perception.self_state)
            await proc.run(ctx)

        assert pile not in exp.piles

    @pytest.mark.asyncio
    async def test_falls_back_to_ground_ore_when_no_piles(self):
        """Without expedition piles, behave like before: use `ground_ore` arg."""
        from anima.planner.helpers import _PickUpAndSmelt

        ctx = _make_ctx()
        # No expedition on blackboard
        ore = MagicMock(
            serial=0xDEAD0002, graphic=0x19B9, amount=2, container=0,
            x=100, y=200, z=0,
        )
        ctx.perception.world.items = {ore.serial: ore}
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 200

        proc = _PickUpAndSmelt(ground_ore=[ore], ss=ctx.perception.self_state)
        result = await proc.run(ctx)
        # Picked up from the legacy path
        assert result.success is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestPickUpAndSmeltWithExpedition -v`
Expected: FAIL — current `_PickUpAndSmelt` doesn't look at expedition.

- [ ] **Step 3: Update `_PickUpAndSmelt` in `helpers.py`**

Replace `_PickUpAndSmelt.run` in `anima/planner/helpers.py` with a version that prefers remembered piles. Full replacement:

```python
    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        expedition = ctx.blackboard.get("expedition")

        # --- Path A: expedition with remembered piles ---
        if expedition is not None and expedition.piles:
            # Nearest pile first
            piles_sorted = sorted(
                expedition.piles,
                key=lambda p: max(abs(p.x - ss.x), abs(p.y - ss.y)),
            )
            target_pile = piles_sorted[0]
            pile_dist = max(abs(target_pile.x - ss.x), abs(target_pile.y - ss.y))
            if pile_dist > 2:
                arrived = await go_to(ctx, target_pile.x, target_pile.y)
                if not arrived:
                    logger.info(
                        "pile_unreachable_removed",
                        pos=f"({target_pile.x},{target_pile.y})",
                    )
                    expedition.mark_pile_collected(target_pile)
                    return ProcedureResult(
                        success=False,
                        reason=FailureReason.BLOCKED,
                        message=f"could not reach pile ({target_pile.x},{target_pile.y})",
                    )

            # Find ore within 2 tiles of current position
            nearby_ore = [
                it for it in ctx.perception.world.items.values()
                if it.graphic in (0x19B7, 0x19B8, 0x19B9, 0x19BA)
                and it.container == 0
                and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2
            ]
            if not nearby_ore:
                logger.info(
                    "pile_empty_removed",
                    pos=f"({target_pile.x},{target_pile.y})",
                )
                expedition.mark_pile_collected(target_pile)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="pile empty at target",
                )

            picked = await self._pickup_into_backpack(ctx, nearby_ore, backpack)
            if picked > 0:
                expedition.mark_pile_collected(target_pile)

            if picked == 0:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="no ore picked up at pile",
                )

            await self._walk_to_forge(ctx)
            return ProcedureResult(
                success=True,
                message=f"Picked up {picked} ore at pile ({target_pile.x},{target_pile.y}), heading to forge",
                next_suggestion="smelt_ore",
            )

        # --- Path B: legacy ground-ore fallback (no expedition / no piles) ---
        fail_counts: dict[int, int] = ctx.blackboard.setdefault(
            "_ore_pickup_fails", {}
        )
        junk: set[int] = ctx.blackboard.setdefault("_junk_ore_serials", set())

        picked = 0
        hard_failures: list[int] = []
        for ore in self._ore_items:
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break
            ore_item = ctx.perception.world.items.get(ore.serial)
            if not ore_item or ore_item.container != 0:
                continue
            dist = max(abs(ore_item.x - ss.x), abs(ore_item.y - ss.y))
            if dist > 2:
                logger.info("ore_too_far", serial=f"0x{ore_item.serial:08X}", dist=dist)
                continue
            await ctx.conn.send_packet(build_pick_up(ore_item.serial, ore_item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(ore_item.serial, container=backpack)
            )
            await asyncio.sleep(0.5)
            ore_check = ctx.perception.world.items.get(ore_item.serial)
            if ore_check and ore_check.container == backpack:
                picked += 1
                fail_counts.pop(ore_item.serial, None)
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_success(ore_item.serial)
                logger.info("picked_up_ore", serial=f"0x{ore_item.serial:08X}", amount=ore_item.amount)
            elif ore_check and ore_check.container == 0:
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_failure(ore_item.serial)
                    opened = breaker.is_open(ore_item.serial)
                    fails = breaker.failure_count(ore_item.serial)
                else:
                    fails = fail_counts.get(ore_item.serial, 0) + 1
                    fail_counts[ore_item.serial] = fails
                    opened = fails >= 2
                logger.warning(
                    "ore_pickup_failed",
                    serial=f"0x{ore_item.serial:08X}",
                    reason="still on ground after pick_up",
                    fails=fails,
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
            else:
                picked += 1
                fail_counts.pop(ore_item.serial, None)
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_success(ore_item.serial)
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                    note="item merged or removed",
                )

        if picked == 0:
            if hard_failures:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"pickup refused by server, marked {len(hard_failures)} ore as junk",
                )
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no ore to pick up",
            )

        await self._walk_to_forge(ctx)
        return ProcedureResult(
            success=True,
            message=f"Picked up {picked} ore stacks, heading to forge",
            next_suggestion="smelt_ore",
        )

    async def _pickup_into_backpack(self, ctx, ore_items, backpack) -> int:
        """Pick each ore in the list into the backpack, respecting weight limit."""
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        picked = 0
        for ore_item in ore_items:
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break
            current = ctx.perception.world.items.get(ore_item.serial)
            if not current or current.container != 0:
                continue
            await ctx.conn.send_packet(build_pick_up(ore_item.serial, ore_item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(ore_item.serial, container=backpack)
            )
            await asyncio.sleep(0.5)
            verify = ctx.perception.world.items.get(ore_item.serial)
            if verify and verify.container == backpack:
                picked += 1
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                )
            elif verify is None:
                picked += 1
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                    note="item merged or removed",
                )
        return picked

    async def _walk_to_forge(self, ctx) -> None:
        """Best-effort walk to the nearest forge/blacksmith location."""
        from anima.action.movement import go_to
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state
        forge = None
        best_dist = 999_999
        for loc in ALL_LOCATIONS:
            if "forge" in loc.name.lower() or "blacksmith" in loc.name.lower():
                d = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if d < best_dist:
                    best_dist = d
                    forge = loc
        if forge and best_dist > 3:
            logger.info("moving_to_forge", target=forge.name, dist=best_dist)
            await go_to(ctx, forge.x, forge.y)
```

*Notes:*
- The two helper methods (`_pickup_into_backpack`, `_walk_to_forge`) dedupe logic shared between paths A and B.
- The legacy block is lifted verbatim from the existing code so Path B behaves exactly as today when the expedition is absent or its `piles` list is empty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestPickUpAndSmeltWithExpedition -v`
Expected: PASS (all 4)

Run the full planner test suite: `uv run pytest tests/test_planner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anima/planner/helpers.py tests/test_planner.py
git commit -m "_PickUpAndSmelt prefers remembered pile positions

When the expedition has piles, walk to the nearest one, pick up
ore within 2 tiles, mark the pile collected. Legacy ground-ore
behavior preserved as a fallback when expedition is absent/empty."
```

---

## Task 6: Gate Priority 3/3b by phase, add MINING → COLLECTING transition

**Files:**
- Modify: `anima/planner/planner.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`:

```python
class TestPhaseGatedSmelt:
    @pytest.mark.asyncio
    async def test_mining_phase_ignores_ground_ore_pickup(self):
        """In MINING phase, two ore on the ground does NOT trigger pick_up_ore_and_smelt."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)

        ctx = _make_ctx()
        # Two ore on the ground near player
        ore = MagicMock(serial=0xA1, graphic=0x19B9, amount=2, container=0, x=100, y=200)
        ctx.perception.world.items = {ore.serial: ore}

        proc = await planner._select_procedure(ctx)
        # Should not be _PickUpAndSmelt
        assert getattr(proc, "name", "") != "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_collecting_phase_does_trigger_pickup(self):
        """In COLLECTING phase with piles, _PickUpAndSmelt is selected."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        proc = await planner._select_procedure(ctx)
        assert getattr(proc, "name", "") == "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_transitions_mining_to_collecting_when_scan_empty(self):
        """When mining scan finds nothing and piles exist, phase flips to COLLECTING."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore", can=False))  # mine not available
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        # Mock the scan helper to say "no banks available"
        with patch.object(planner, "_scan_has_mineable_bank", return_value=False):
            await planner._select_procedure(ctx)

        assert planner._expedition.phase == Phase.COLLECTING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestPhaseGatedSmelt -v`
Expected: FAIL (scan helper doesn't exist; Priority 3b still fires in MINING)

- [ ] **Step 3: Add scan helper to planner**

In `anima/planner/planner.py`, add a method on the `Planner` class:

```python
    def _scan_has_mineable_bank(self, ctx) -> bool:
        """True if at least one un-depleted mineable bank is within MOVE_RADIUS."""
        from anima.skills.gathering.mine import _find_mineable_tile
        return _find_mineable_tile(ctx) is not None
```

- [ ] **Step 4: Phase-gate Priority 3/3b and add the transition**

Locate the Priority 3 / 3b block in `_select_procedure` (around lines 533–557). Wrap it so the block only runs in COLLECTING. Immediately *before* the block, add the MINING → COLLECTING transition check. Replace:

```python
        # --- Priority 3: Batch smelt — mine multiple spots first ---
        if smeltable_ore >= BATCH_SMELT_ORE:
            ...
        # --- Priority 3b: Ore on ground nearby → pick up then go smelt ---
        ...
```

with:

```python
        # --- MINING → COLLECTING transition ---
        expedition = self._expedition
        if expedition.should_start_collecting(
            scan_empty=not self._scan_has_mineable_bank(ctx),
        ):
            expedition.transition_to(Phase.COLLECTING)

        # --- Priority 3: Batch smelt (only in COLLECTING) ---
        if expedition.phase == Phase.COLLECTING and smeltable_ore >= BATCH_SMELT_ORE:
            proc = _get_proc("smelt_ore")
            if proc and await proc.can_start(ctx):
                _intent(f"수거 단계, 광석 {smeltable_ore}개 → 일괄 제련")
                return proc
            _intent(f"수거 단계, 광석 {smeltable_ore}개, 용광로 필요 → 이동")
            return await self._roaming.move_to_location(ctx, "forge", "blacksmith")

        # --- Priority 3b: Collection tour — pick up next pile ---
        if expedition.phase == Phase.COLLECTING:
            can_carry_more = ss.weight_max == 0 or ss.weight <= ss.weight_max - 50
            if (can_carry_more
                    and self.continuation_hint != "smelt_ore"
                    and "pick_up_ore_and_smelt" not in skip_bb):
                ground_ore = self._find_ground_ore(ctx, ss)
                if expedition.piles or (ground_ore and sum(it.amount for it in ground_ore) >= 2):
                    _intent(
                        f"수거 투어: 더미 {len(expedition.piles)}개 → 다음 더미 줍기"
                    )
                    return _PickUpAndSmelt(ground_ore, ss)
```

*Note: the legacy two-ore threshold still triggers when the expedition is in COLLECTING but has no remembered piles — this covers the restart-recovery case.*

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestPhaseGatedSmelt -v`
Expected: PASS

Run the full planner test suite: `uv run pytest tests/test_planner.py -v`
Expected: PASS — the existing batch smelt tests may need a phase setup; if any fail, it's because they relied on Priority 3 firing outside of COLLECTING. Update those tests to set `planner._expedition.transition_to(Phase.COLLECTING)` as part of their arrangement.

- [ ] **Step 6: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Phase-gate batch smelt; add MINING → COLLECTING transition

Priority 3/3b now only fire during COLLECTING. A new per-tick check
transitions MINING → COLLECTING when no banks are mineable within
MOVE_RADIUS and the expedition has at least one remembered pile."
```

---

## Task 7: COLLECTING → CRAFTING_TRIP / COLLECTING → MINING transitions

**Files:**
- Modify: `anima/planner/planner.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`:

```python
class TestCollectingTransitions:
    @pytest.mark.asyncio
    async def test_collecting_to_crafting_trip_when_ingots_enough(self):
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = []  # all collected

        ctx = _make_ctx()
        # Give the agent 16 iron ingots + tongs
        _add_item(ctx, 0xB1, INGOT, amount=16)
        _add_item(ctx, 0xB2, TONGS, amount=1)
        _add_item(ctx, 0xB3, PICKAXE, amount=1)

        await planner._select_procedure(ctx)
        assert planner._expedition.phase == Phase.CRAFTING_TRIP

    @pytest.mark.asyncio
    async def test_collecting_back_to_mining_when_nothing_triggers_craft(self):
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = []

        ctx = _make_ctx()
        _add_item(ctx, 0xB4, PICKAXE, amount=1)
        # few ingots, light weight

        await planner._select_procedure(ctx)
        assert planner._expedition.phase == Phase.MINING

    @pytest.mark.asyncio
    async def test_collecting_stays_if_piles_not_empty(self):
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=1, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        _add_item(ctx, 0xB5, INGOT, amount=50)  # would trigger craft if piles empty

        await planner._select_procedure(ctx)
        assert planner._expedition.phase == Phase.COLLECTING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestCollectingTransitions -v`
Expected: FAIL — no transition logic for these yet.

- [ ] **Step 3: Add the transitions**

In `anima/planner/planner.py`, immediately after the Priority 3/3b block from Task 6 (still inside `_select_procedure`), add:

```python
        # --- COLLECTING → CRAFTING_TRIP or COLLECTING → MINING ---
        if expedition.phase == Phase.COLLECTING and not expedition.piles:
            weight_ratio = (ss.weight / ss.weight_max) if ss.weight_max > 0 else 0.0
            if expedition.should_leave_mine(
                ingot_count=ingot_count,
                weight_ratio=weight_ratio,
                has_pickaxe=has_mining_tool,
            ):
                expedition.transition_to(Phase.CRAFTING_TRIP)
            else:
                expedition.transition_to(Phase.MINING)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestCollectingTransitions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Add COLLECTING exit transitions

When piles are empty and craft triggers hold (enough ingots, overweight
with ingots, or no pickaxe) → CRAFTING_TRIP. Otherwise → MINING."
```

---

## Task 8: Gate Priority 5 (craft) by CRAFTING_TRIP, add CRAFTING_TRIP → MINING

**Files:**
- Modify: `anima/planner/planner.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`:

```python
class TestCraftingTripPhase:
    @pytest.mark.asyncio
    async def test_mining_phase_does_not_craft_even_with_ingots(self):
        """In MINING phase, having 16+ ingots does NOT route to craft."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)

        ctx = _make_ctx()
        _add_item(ctx, 0xC1, INGOT, amount=16)
        _add_item(ctx, 0xC2, TONGS, amount=1)
        _add_item(ctx, 0xC3, PICKAXE, amount=1)

        proc = await planner._select_procedure(ctx)
        assert getattr(proc, "name", "") != "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_crafting_trip_phase_runs_craft(self):
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)

        ctx = _make_ctx()
        _add_item(ctx, 0xC4, INGOT, amount=16)
        _add_item(ctx, 0xC5, TONGS, amount=1)

        proc = await planner._select_procedure(ctx)
        assert getattr(proc, "name", "") == "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_crafting_trip_back_to_mining_when_done(self):
        """Ingots < 4, no crafted items, near home_base → MINING + cycles_completed += 1."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)
        planner._expedition.home_base = (100, 200)
        start_cycles = planner._expedition.cycles_completed

        ctx = _make_ctx()
        # Very few ingots, no crafted items, player at home_base
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 200
        _add_item(ctx, 0xD1, PICKAXE, amount=1)

        await planner._select_procedure(ctx)
        assert planner._expedition.phase == Phase.MINING
        assert planner._expedition.cycles_completed == start_cycles + 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestCraftingTripPhase -v`
Expected: FAIL

- [ ] **Step 3: Gate Priority 5 and add the return transition**

Locate the Priority 5 block in `_select_procedure` (around lines 720–763). Wrap the "batch craft" branch in `if expedition.phase == Phase.CRAFTING_TRIP:` so it only activates during the crafting trip. Example edit — replace:

```python
        # --- Priority 5: Batch craft — accumulate ingots first ---
        if ingot_count >= BATCH_CRAFT_INGOTS:
```

with:

```python
        # --- Priority 5: Batch craft (only during CRAFTING_TRIP) ---
        if expedition.phase == Phase.CRAFTING_TRIP and ingot_count >= BATCH_CRAFT_INGOTS:
```

Immediately after the end of the Priority 5 block (and its 5b sibling, just before the sell / bank priorities), add the return transition:

```python
        # --- CRAFTING_TRIP → MINING ---
        if expedition.phase == Phase.CRAFTING_TRIP and expedition.home_base is not None:
            near_home = max(
                abs(ss.x - expedition.home_base[0]),
                abs(ss.y - expedition.home_base[1]),
            ) <= 30
            if expedition.should_return_to_mine(
                ingot_count=ingot_count,
                crafted_count=crafted_count,
                near_home=near_home,
            ):
                expedition.cycles_completed += 1
                logger.info(
                    "expedition_cycle_complete",
                    cycles=expedition.cycles_completed,
                    duration_s=time.time() - expedition.phase_started_at,
                )
                expedition.transition_to(Phase.MINING)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestCraftingTripPhase -v`
Expected: PASS

Run the full planner suite: `uv run pytest tests/test_planner.py -v`
Expected: PASS. If any existing batch-craft test fails, it's because it ran Priority 5 without the phase. Update those tests to set `planner._expedition.transition_to(Phase.CRAFTING_TRIP)` in their arrangement.

- [ ] **Step 5: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Phase-gate craft; add CRAFTING_TRIP → MINING transition

Priority 5 (batch craft) only runs in CRAFTING_TRIP. When ingots are
disposed and the agent is within 30 tiles of home_base, transition
back to MINING and increment cycles_completed."
```

---

## Task 9: Watchdog + observability + activity entries

**Files:**
- Modify: `anima/planner/planner.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_planner.py`:

```python
class TestExpeditionWatchdog:
    @pytest.mark.asyncio
    async def test_stuck_phase_resets_to_idle(self):
        """If the current phase has been active > 10 min, transition to IDLE."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.phase_started_at = time.time() - 700  # 11m40s ago
        planner._expedition.piles = [
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        await planner._select_procedure(ctx)
        assert planner._expedition.phase == Phase.IDLE
        assert planner._expedition.piles == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py::TestExpeditionWatchdog -v`
Expected: FAIL — watchdog not implemented.

- [ ] **Step 3: Implement the watchdog**

In `anima/planner/planner.py`, near the top of `_select_procedure`, immediately after the line that publishes the expedition to the blackboard and calls `prune_stale_piles()`, add:

```python
        if (self._expedition.phase != Phase.IDLE
                and self._expedition.watchdog_expired(max_phase_s=600.0)):
            logger.warning(
                "expedition_watchdog",
                phase=self._expedition.phase.value,
                stuck_s=time.time() - self._expedition.phase_started_at,
            )
            self._expedition.piles.clear()
            self._expedition.transition_to(Phase.IDLE)
```

- [ ] **Step 4: Add activity-log entry on cycle complete**

Extend the cycle-complete section in Task 8's edit. Replace:

```python
                expedition.cycles_completed += 1
                logger.info(
                    "expedition_cycle_complete",
                    cycles=expedition.cycles_completed,
                    duration_s=time.time() - expedition.phase_started_at,
                )
                expedition.transition_to(Phase.MINING)
```

with:

```python
                expedition.cycles_completed += 1
                duration = time.time() - expedition.phase_started_at
                logger.info(
                    "expedition_cycle_complete",
                    cycles=expedition.cycles_completed,
                    duration_s=duration,
                )
                if ctx.memory_db is not None:
                    try:
                        ctx.memory_db.log_activity(
                            topic="expedition.cycle_complete",
                            message=f"✓ 원정 사이클 {expedition.cycles_completed}회 완료 ({duration:.0f}s)",
                            importance=3,
                        )
                    except Exception:
                        pass  # activity logging must never break the planner
                expedition.transition_to(Phase.MINING)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py::TestExpeditionWatchdog -v`
Expected: PASS

Run the full suite: `uv run pytest tests/ -v`
Expected: PASS (fix any remaining test setup that assumed Priority 5 runs without phase; see Tasks 6–8 notes).

- [ ] **Step 6: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Expedition watchdog + cycle activity log

10-minute stuck phase → IDLE + clear piles. CRAFTING_TRIP → MINING
now emits an activity-log entry so the supervisor can track progress."
```

---

## Task 10: Integration test — one full cycle

**Files:**
- Test: `tests/test_expedition_integration.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_expedition_integration.py`:

```python
"""Integration test — one MINING → COLLECTING → CRAFTING_TRIP → MINING cycle."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.planner.expedition import Phase, PileRecord
from anima.planner.planner import Planner
from anima.procedures.base import Procedure, ProcedureRegistry, ProcedureResult


class _Stub(Procedure):
    def __init__(self, name: str, can: bool = True):
        self.name = name
        self.description = f"Stub {name}"
        self._can = can

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(success=True, message=f"{self.name} done")


def _ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 2460, 558, 5
    ss.hits, ss.hits_max = 100, 100
    ss.weight, ss.weight_max = 100, 400
    ss.gold = 0
    ss.equipment = {0x15: 0x101}
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    ctx.memory_db = None
    ctx.persona = MagicMock(name="Grimm")
    return ctx


def _add(ctx, serial, graphic, amount=1):
    it = MagicMock(
        container=0x101, graphic=graphic, amount=amount, hue=0, serial=serial,
    )
    ctx.perception.world.items[serial] = it


PICKAXE, ORE, INGOT, TONGS = 0x0E86, 0x19B9, 0x1BF2, 0x0FBB


@pytest.mark.asyncio
async def test_one_full_expedition_cycle():
    """
    Walk through the full state machine:
    1. IDLE — no procedure selected phase-wise, planner picks mine_ore via Priority 7
    2. Simulate note_ore_mined → phase = MINING, piles += 1
    3. Force scan_empty=True → MINING → COLLECTING
    4. Empty piles + enough ingots → COLLECTING → CRAFTING_TRIP
    5. Crafting done (no ingots, near home) → CRAFTING_TRIP → MINING, cycles += 1
    """
    reg = ProcedureRegistry()
    reg.register(_Stub("heal_self", can=False))
    reg.register(_Stub("mine_ore"))
    reg.register(_Stub("smelt_ore"))
    reg.register(_Stub("craft_blacksmith"))
    reg.register(_Stub("sell_to_vendor"))
    planner = Planner(reg)
    ctx = _ctx()
    _add(ctx, 0xA0, PICKAXE, 1)

    # Stage 1: IDLE → select something that can mine
    proc1 = await planner._select_procedure(ctx)
    assert planner._expedition.phase in (Phase.IDLE, Phase.MINING)
    assert proc1 is not None

    # Stage 2: simulate a successful mine (the hook runs inside mine.py in prod)
    planner._expedition.note_ore_mined(x=2460, y=558, bank_key=(307, 69))
    assert planner._expedition.phase == Phase.MINING
    assert len(planner._expedition.piles) == 1

    # Stage 3: no more banks → MINING → COLLECTING
    with patch.object(planner, "_scan_has_mineable_bank", return_value=False):
        await planner._select_procedure(ctx)
    assert planner._expedition.phase == Phase.COLLECTING

    # Stage 4: empty piles + 16 ingots → CRAFTING_TRIP
    planner._expedition.piles = []
    _add(ctx, 0xA1, INGOT, 16)
    _add(ctx, 0xA2, TONGS, 1)
    await planner._select_procedure(ctx)
    assert planner._expedition.phase == Phase.CRAFTING_TRIP

    # Stage 5: crafting done — no ingots, no crafted items, at home_base → MINING
    # Remove the ingot stack
    ctx.perception.world.items.pop(0xA1, None)
    ctx.perception.self_state.x, ctx.perception.self_state.y = 2460, 558
    start_cycles = planner._expedition.cycles_completed
    await planner._select_procedure(ctx)
    assert planner._expedition.phase == Phase.MINING
    assert planner._expedition.cycles_completed == start_cycles + 1
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_expedition_integration.py -v`
Expected: PASS

- [ ] **Step 3: Final full test-suite run**

Run: `uv run pytest tests/ -v`
Expected: PASS — all existing tests plus the new ones.

Run the linter:

Run: `uv run ruff check anima/planner/expedition.py anima/planner/planner.py anima/planner/helpers.py anima/skills/gathering/mine.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/test_expedition_integration.py
git commit -m "Integration test: full expedition cycle

Walks through IDLE → MINING → COLLECTING → CRAFTING_TRIP → MINING
exercising the hooks in mine.py and the phase transitions in the
planner. Closes the batch-mining-expedition plan."
```

---

## Post-Implementation Verification (not a task — engineer checks)

1. Restart the live agent and observe `data/anima.log` for:
   - `expedition_phase` structured events on every phase change
   - `expedition_cycle_complete` at least twice within one hour on the Minoc mining loop
2. Confirm `planner_health_loop_detected` warning frequency drops to under one per 10 minutes.
3. If `expedition_watchdog` fires more than once per hour, investigate — the 10-minute cap may be too aggressive for the real path distances.
