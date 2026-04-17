# Metrics Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the metrics pipeline that captures agent events, rolls them up hourly/daily, surfaces results on supervisor stdio and a CLI, and fires threshold alerts on regressions.

**Architecture:** New `anima/monitor/metrics_pipeline/` package with three classes (Collector, Aggregator, AlertDetector) plus a module-level `record()` helper. Collector consumes state.json diffs, bus events, and action_logs polling; Aggregator rolls into hourly/daily JSONL; AlertDetector checks threshold rules. CLI at `tools/metrics.py` queries the JSONL files offline. Supervisor subscribes to new bus topics for stdio surfacing.

**Tech Stack:** Python 3.12, `asyncio`, `structlog`, `pytest` + `pytest-asyncio`, `aiosqlite` (already in use by memory DB), existing `anima.core.bus.EventBus`.

---

## File Structure

**Create:**
- `anima/monitor/metrics_pipeline/__init__.py` — re-exports + `record()` helper
- `anima/monitor/metrics_pipeline/collector.py` — `MetricsCollector`
- `anima/monitor/metrics_pipeline/aggregator.py` — `MetricsAggregator`
- `anima/monitor/metrics_pipeline/alerts.py` — `MetricsAlertDetector`
- `tools/metrics.py` — CLI tool
- `tests/monitor/__init__.py` (if missing)
- `tests/monitor/test_metrics_record.py`
- `tests/monitor/test_metrics_collector.py`
- `tests/monitor/test_metrics_aggregator.py`
- `tests/monitor/test_metrics_alerts.py`
- `tests/tools/__init__.py` (if missing)
- `tests/tools/test_metrics_cli.py`

**Modify:**
- `anima/core/avatar.py` — instantiate + start Collector/Aggregator/AlertDetector
- `tools/supervisor.py` — emit `supervisor.auto_recover` bus event; subscribe to `metrics.hourly_complete` + `metrics.alert` for stdio

**Untouched** (already emit needed events):
- `anima/planner/planner.py` — `expedition.cycle_complete` and `planner.death` already publish to bus

**Data paths (runtime-created, gitignored):**
- `data/metrics_events.jsonl`
- `data/metrics_hourly.jsonl`
- `data/metrics_daily.jsonl`
- `data/metrics_alerts.jsonl`

---

## Task 1: `record()` helper + event stream writer

**Files:**
- Create: `anima/monitor/metrics_pipeline/__init__.py`
- Test: `tests/monitor/test_metrics_record.py`

- [ ] **Step 1: Write the failing test**

Create `tests/monitor/test_metrics_record.py`:

```python
"""Tests for anima.monitor.metrics_pipeline.record()."""
from __future__ import annotations

import json
from pathlib import Path

from anima.monitor.metrics_pipeline import record


def test_record_appends_single_line(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("procedure_end", events_file=events_file, proc="mine_ore", success=True)
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "procedure_end"
    assert entry["proc"] == "mine_ore"
    assert entry["success"] is True
    assert isinstance(entry["ts"], float)


def test_record_appends_multiple_events(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("gold_delta", events_file=events_file, amount=10, reason="sell")
    record("gold_delta", events_file=events_file, amount=-5, reason="buy")
    record("death", events_file=events_file, pos=[2553, 496], hp_before=12)
    lines = events_file.read_text().splitlines()
    assert len(lines) == 3
    types = [json.loads(l)["type"] for l in lines]
    assert types == ["gold_delta", "gold_delta", "death"]


def test_record_creates_parent_directory(tmp_path: Path):
    events_file = tmp_path / "nested" / "dir" / "metrics_events.jsonl"
    record("test_event", events_file=events_file, foo="bar")
    assert events_file.exists()
    entry = json.loads(events_file.read_text().strip())
    assert entry["foo"] == "bar"


def test_record_timestamp_monotonic(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("a", events_file=events_file)
    record("b", events_file=events_file)
    lines = events_file.read_text().splitlines()
    ts1 = json.loads(lines[0])["ts"]
    ts2 = json.loads(lines[1])["ts"]
    assert ts2 >= ts1  # at least non-decreasing


def test_record_ignores_write_errors(tmp_path: Path):
    """record() must never raise even when the file cannot be opened."""
    events_file = tmp_path / "will_fail"
    events_file.mkdir()  # a directory with this name — open('w+') will fail
    # Must not raise
    record("test", events_file=events_file, value=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/dkkang/dev/uo/anima && .venv/bin/pytest tests/monitor/test_metrics_record.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'anima.monitor.metrics_pipeline'`

- [ ] **Step 3: Implement `record()`**

Create `anima/monitor/metrics_pipeline/__init__.py`:

```python
"""Metrics pipeline — event stream + hourly/daily rollups + alerts.

See docs/superpowers/specs/2026-04-17-metrics-pipeline-design.md.

Three concerns:
  - collector.py: MetricsCollector (state-poll + bus + action_logs poll)
  - aggregator.py: MetricsAggregator (hourly + daily rollups + retention)
  - alerts.py: MetricsAlertDetector (threshold rules)

Module-level record() is the manual-emit escape hatch for callers that
cannot be reached by the automatic pipelines.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).parent.parent.parent.parent
DEFAULT_EVENTS_FILE = ROOT / "data" / "metrics_events.jsonl"


def record(
    event_type: str,
    *,
    events_file: Path | None = None,
    **payload: Any,
) -> None:
    """Append one event to the raw event stream.

    Safe for use from any loop — never raises. Uses append-mode write
    which is OS-atomic for payloads under PIPE_BUF (typically 4 KB).
    """
    target = events_file or DEFAULT_EVENTS_FILE
    row = {"ts": time.time(), "type": event_type, **payload}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("metrics_record_failed", event_type=event_type, error=str(e))


__all__ = ["record", "DEFAULT_EVENTS_FILE"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_record.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/__init__.py tests/monitor/test_metrics_record.py
git commit -m "Add metrics_pipeline.record() — raw event stream writer

Module-level record() appends a single JSON line to
data/metrics_events.jsonl. Safe-fails on any I/O error so a broken
metrics file can never take down the agent.

Spec: docs/superpowers/specs/2026-04-17-metrics-pipeline-design.md
(Task 1 of the metrics pipeline plan.)"
```

---

## Task 2: `MetricsCollector` — state.json diff

**Files:**
- Create: `anima/monitor/metrics_pipeline/collector.py`
- Test: `tests/monitor/test_metrics_collector.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/monitor/test_metrics_collector.py`:

```python
"""Tests for MetricsCollector."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from anima.monitor.metrics_pipeline.collector import MetricsCollector


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_events.jsonl"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines()]


def _make_state(**status_overrides) -> dict:
    default_status = {
        "hp": 112, "hp_max": 112,
        "gold": 50, "weight": 100, "weight_max": 474,
        "x": 2553, "y": 496,
    }
    default_status.update(status_overrides)
    return {
        "ts": 0.0,
        "status": default_status,
        "inventory": [],
        "skills": {"list": []},
    }


class TestStateDiff:
    def test_gold_delta_emitted_on_increase(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=50), _make_state(gold=75))
        events = _read(events_file)
        assert len(events) == 1
        assert events[0]["type"] == "gold_delta"
        assert events[0]["amount"] == 25

    def test_gold_delta_emitted_on_decrease(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=100), _make_state(gold=70))
        events = _read(events_file)
        assert events[0]["amount"] == -30

    def test_no_gold_delta_when_unchanged(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=50), _make_state(gold=50))
        assert _read(events_file) == []

    def test_death_on_hp_zero_transition(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(
            _make_state(hp=20, x=2553, y=496),
            _make_state(hp=0, x=2553, y=496),
        )
        events = _read(events_file)
        types = [e["type"] for e in events]
        assert "death" in types
        death = next(e for e in events if e["type"] == "death")
        assert death["pos"] == [2553, 496]
        assert death["hp_before"] == 20

    def test_no_duplicate_death_while_dead(self, events_file: Path):
        """Staying dead at hp=0 must not fire a second death event."""
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(hp=20), _make_state(hp=0))
        col._diff_state(_make_state(hp=0), _make_state(hp=0))
        events = _read(events_file)
        death_events = [e for e in events if e["type"] == "death"]
        assert len(death_events) == 1

    def test_skill_delta(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        before = _make_state()
        before["skills"] = {"list": [{"id": 7, "value": 63.9}]}
        after = _make_state()
        after["skills"] = {"list": [{"id": 7, "value": 64.0}]}
        col._diff_state(before, after)
        events = _read(events_file)
        skill_events = [e for e in events if e["type"] == "skill_delta"]
        assert len(skill_events) == 1
        assert skill_events[0]["skill_id"] == 7
        assert skill_events[0]["from"] == 63.9
        assert skill_events[0]["to"] == 64.0

    def test_first_snapshot_emits_nothing(self, events_file: Path):
        """With no previous snapshot, no diff events should fire."""
        col = MetricsCollector(events_file=events_file)
        col._diff_state(None, _make_state(gold=50))
        assert _read(events_file) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: collector`.

- [ ] **Step 3: Implement `MetricsCollector._diff_state`**

Create `anima/monitor/metrics_pipeline/collector.py`:

```python
"""MetricsCollector — watches state.json, bus, and action_logs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from anima.monitor.metrics_pipeline import record

logger = structlog.get_logger()


class MetricsCollector:
    """Collects events from three sources into metrics_events.jsonl.

    Sources:
      1. state.json periodic diff (gold, skills, inventory, HP)
      2. EventBus subscriptions (expedition, death, auto_recover, phase)
      3. action_logs table polling (procedure_end events)
    """

    def __init__(self, events_file: Path | None = None) -> None:
        self.events_file = events_file
        self._last_state: dict | None = None
        self._was_dead: bool = False  # tracks hp==0 edge

    def _emit(self, event_type: str, **payload: Any) -> None:
        record(event_type, events_file=self.events_file, **payload)

    def _diff_state(self, prev: dict | None, curr: dict) -> None:
        """Compare two state.json snapshots and emit events for diffs."""
        if prev is None:
            # First snapshot — just remember it for next call.
            self._was_dead = _status(curr).get("hp", 1) <= 0
            return

        prev_s = _status(prev)
        curr_s = _status(curr)

        # Gold delta
        prev_gold = prev_s.get("gold", 0)
        curr_gold = curr_s.get("gold", 0)
        if curr_gold != prev_gold:
            self._emit("gold_delta", amount=curr_gold - prev_gold)

        # Death edge (hp falling to zero from >0)
        prev_hp = prev_s.get("hp", 0)
        curr_hp = curr_s.get("hp", 0)
        if curr_hp <= 0 and prev_hp > 0:
            pos = [curr_s.get("x", 0), curr_s.get("y", 0)]
            self._emit("death", pos=pos, hp_before=prev_hp)
            self._was_dead = True
        elif curr_hp > 0:
            self._was_dead = False

        # Skill deltas
        prev_skills = _skill_map(prev)
        curr_skills = _skill_map(curr)
        for sid, curr_val in curr_skills.items():
            prev_val = prev_skills.get(sid)
            if prev_val is not None and prev_val != curr_val:
                self._emit(
                    "skill_delta",
                    skill_id=sid,
                    **{"from": prev_val, "to": curr_val},
                )


def _status(snapshot: dict) -> dict:
    return snapshot.get("status") or {}


def _skill_map(snapshot: dict) -> dict[int, float]:
    """Extract {skill_id: value} from a state snapshot."""
    skills_block = snapshot.get("skills") or {}
    entries = skills_block.get("list") or []
    return {int(e["id"]): float(e["value"]) for e in entries if "id" in e and "value" in e}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_collector.py::TestStateDiff -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/collector.py tests/monitor/test_metrics_collector.py
git commit -m "MetricsCollector: state.json diff → gold/skill/death events

Pure logic layer: given two state snapshots, emit the delta events.
Start/stop lifecycle and bus subscriptions come next. The first
snapshot is silent (no events) so metrics only appear for real
changes."
```

---

## Task 3: `MetricsCollector` — bus subscriptions + action_logs poller

**Files:**
- Modify: `anima/monitor/metrics_pipeline/collector.py`
- Modify: `tests/monitor/test_metrics_collector.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/monitor/test_metrics_collector.py`:

```python
class TestBusSubscriptions:
    def test_cycle_complete_emitted(self, events_file: Path):
        from anima.core.bus import EventBus

        bus = EventBus()
        col = MetricsCollector(events_file=events_file)
        col.attach_bus(bus)
        bus.publish("expedition.cycle_complete", {"cycles": 2, "duration_s": 123.0})
        events = _read(events_file)
        cycle_events = [e for e in events if e["type"] == "cycle_complete"]
        assert len(cycle_events) == 1
        assert cycle_events[0]["cycles_completed"] == 2
        assert cycle_events[0]["duration_s"] == 123.0

    def test_planner_death_emitted(self, events_file: Path):
        from anima.core.bus import EventBus

        bus = EventBus()
        col = MetricsCollector(events_file=events_file)
        col.attach_bus(bus)
        bus.publish("planner.death", {"message": "☠", "importance": 5})
        events = _read(events_file)
        death_events = [e for e in events if e["type"] == "death"]
        # Bus-sourced death carries no pos/hp_before; just verify we recorded.
        assert len(death_events) >= 1

    def test_supervisor_auto_recover_emitted(self, events_file: Path):
        from anima.core.bus import EventBus

        bus = EventBus()
        col = MetricsCollector(events_file=events_file)
        col.attach_bus(bus)
        bus.publish("supervisor.auto_recover", {"reason": "planner idle loop"})
        events = _read(events_file)
        stuck = [e for e in events if e["type"] == "stuck_event"]
        assert len(stuck) == 1
        assert stuck[0]["reason"] == "planner idle loop"

    def test_phase_transition_emitted(self, events_file: Path):
        from anima.core.bus import EventBus

        bus = EventBus()
        col = MetricsCollector(events_file=events_file)
        col.attach_bus(bus)
        bus.publish("expedition.phase", {"from_": "mining", "to": "collecting"})
        events = _read(events_file)
        phase = [e for e in events if e["type"] == "phase_transition"]
        assert len(phase) == 1
        assert phase[0]["from_"] == "mining"
        assert phase[0]["to"] == "collecting"


class TestActionLogsPoll:
    @pytest.mark.asyncio
    async def test_action_log_rows_become_procedure_end_events(
        self, events_file: Path, tmp_path: Path,
    ):
        import aiosqlite

        db_path = tmp_path / "test.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE action_logs (
                    timestamp REAL, agent TEXT, procedure TEXT,
                    location_x INTEGER, location_y INTEGER,
                    result TEXT, message TEXT, duration_ms REAL,
                    details TEXT
                )
            """)
            await db.executemany(
                "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (100.0, "Grimm", "mine_ore", 100, 100, "success", "Mined", 3500.0, "{}"),
                    (200.0, "Grimm", "mine_ore", 100, 100, "blocked", "depleted", 2000.0, "{}"),
                    (300.0, "Grimm", "smelt_ore", 101, 100, "success", "Smelted", 1200.0, "{}"),
                ],
            )
            await db.commit()

        col = MetricsCollector(events_file=events_file)
        n = await col.poll_action_logs(db_path=db_path, since=0.0)
        assert n == 3

        events = _read(events_file)
        proc_events = [e for e in events if e["type"] == "procedure_end"]
        assert len(proc_events) == 3
        assert proc_events[0]["proc"] == "mine_ore"
        assert proc_events[0]["success"] is True
        assert proc_events[0]["duration_ms"] == 3500.0
        assert proc_events[1]["success"] is False

    @pytest.mark.asyncio
    async def test_poll_skips_already_seen_rows(
        self, events_file: Path, tmp_path: Path,
    ):
        import aiosqlite

        db_path = tmp_path / "test.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE action_logs (
                    timestamp REAL, agent TEXT, procedure TEXT,
                    location_x INTEGER, location_y INTEGER,
                    result TEXT, message TEXT, duration_ms REAL,
                    details TEXT
                )
            """)
            await db.execute(
                "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
                (100.0, "Grimm", "mine_ore", 100, 100, "success", "", 0.0, "{}"),
            )
            await db.commit()

        col = MetricsCollector(events_file=events_file)
        await col.poll_action_logs(db_path=db_path, since=0.0)
        # Second poll with the collector's internal checkpoint
        n2 = await col.poll_action_logs(db_path=db_path)
        assert n2 == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_collector.py::TestBusSubscriptions tests/monitor/test_metrics_collector.py::TestActionLogsPoll -v`
Expected: FAIL — `attach_bus` / `poll_action_logs` not defined.

- [ ] **Step 3: Add bus + action_logs logic**

Append to `anima/monitor/metrics_pipeline/collector.py` inside the `MetricsCollector` class:

```python
    # --- Bus subscriptions ---

    def attach_bus(self, bus) -> None:
        """Subscribe to agent events; call once after construction."""
        bus.subscribe("expedition.cycle_complete", self._on_cycle)
        bus.subscribe("expedition.phase", self._on_phase)
        bus.subscribe("planner.death", self._on_death_event)
        bus.subscribe("supervisor.auto_recover", self._on_auto_recover)

    def _on_cycle(self, _topic: str, data: dict) -> None:
        self._emit(
            "cycle_complete",
            cycles_completed=data.get("cycles", 0),
            duration_s=data.get("duration_s", 0.0),
        )

    def _on_phase(self, _topic: str, data: dict) -> None:
        self._emit(
            "phase_transition",
            from_=data.get("from_"),
            to=data.get("to"),
        )

    def _on_death_event(self, _topic: str, data: dict) -> None:
        self._emit(
            "death",
            source="bus",
            message=(data.get("message") or "")[:60],
        )

    def _on_auto_recover(self, _topic: str, data: dict) -> None:
        self._emit("stuck_event", reason=data.get("reason", ""))

    # --- action_logs polling ---

    _action_log_checkpoint: float = 0.0

    async def poll_action_logs(
        self, *, db_path: Path | None = None, since: float | None = None,
    ) -> int:
        """Emit procedure_end events for rows newer than last checkpoint.

        Returns the number of new events emitted.
        """
        import aiosqlite

        if db_path is None:
            from anima.memory.database import DB_FILE as _DEFAULT
            db_path = _DEFAULT
        cutoff = since if since is not None else self._action_log_checkpoint

        count = 0
        max_seen = cutoff
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT timestamp, procedure, result, duration_ms "
                "FROM action_logs WHERE timestamp > ? ORDER BY timestamp",
                (cutoff,),
            )
            async for ts, proc, result, dur in cursor:
                self._emit(
                    "procedure_end",
                    proc=proc,
                    success=(result == "success"),
                    duration_ms=float(dur or 0.0),
                    result=result,
                )
                count += 1
                if ts > max_seen:
                    max_seen = ts

        self._action_log_checkpoint = max_seen
        return count
```

Also update the class header (top of the class, after the class docstring) to include `_action_log_checkpoint: float = 0.0` as a class-level default — already included above inline; verify it appears only once.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_collector.py -v`
Expected: all tests PASS (state diff + bus + action logs).

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/collector.py tests/monitor/test_metrics_collector.py
git commit -m "MetricsCollector: bus subscriptions + action_logs poll

Adds attach_bus() for expedition/death/auto_recover/phase topics and
poll_action_logs() which emits procedure_end events for any action_log
rows newer than the internal checkpoint. Idempotent poll — calling
twice in a row emits zero events the second time.

No async lifecycle yet — that's a later task."
```

---

## Task 4: `MetricsAggregator` — hourly rollup

**Files:**
- Create: `anima/monitor/metrics_pipeline/aggregator.py`
- Test: `tests/monitor/test_metrics_aggregator.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/monitor/test_metrics_aggregator.py`:

```python
"""Tests for MetricsAggregator."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_events.jsonl"


@pytest.fixture
def hourly_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_hourly.jsonl"


@pytest.fixture
def daily_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_daily.jsonl"


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


async def _make_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE action_logs (
                timestamp REAL, agent TEXT, procedure TEXT,
                location_x INTEGER, location_y INTEGER,
                result TEXT, message TEXT, duration_ms REAL,
                details TEXT
            )
        """)
        await db.commit()


async def _insert_log(path: Path, ts: float, proc: str, result: str, dur_ms: float) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, "Grimm", proc, 0, 0, result, "", dur_ms, "{}"),
        )
        await db.commit()


class TestHourlyRollup:
    @pytest.mark.asyncio
    async def test_counts_gold_deltas_and_cycles(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        _write_events(events_file, [
            {"ts": 100.0, "type": "gold_delta", "amount": 50},
            {"ts": 150.0, "type": "gold_delta", "amount": -10},
            {"ts": 200.0, "type": "cycle_complete", "cycles_completed": 1, "duration_s": 100.0},
            {"ts": 250.0, "type": "death", "pos": [0, 0], "hp_before": 10},
            {"ts": 300.0, "type": "stuck_event", "reason": "idle"},
        ])

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

        assert row["gold"]["earned"] == 50
        assert row["gold"]["spent"] == 10
        assert row["gold"]["delta"] == 40
        assert row["cycles_completed"] == 1
        assert row["deaths"] == 1
        assert row["stuck_events"] == 1

    @pytest.mark.asyncio
    async def test_aggregates_procedures_from_action_logs(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        await _insert_log(db, 100.0, "mine_ore", "success", 3500.0)
        await _insert_log(db, 200.0, "mine_ore", "success", 3700.0)
        await _insert_log(db, 300.0, "mine_ore", "blocked", 1000.0)
        await _insert_log(db, 400.0, "smelt_ore", "success", 2000.0)

        events_file.write_text("")
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

        assert row["procedures"]["mine_ore"]["ok"] == 2
        assert row["procedures"]["mine_ore"]["fail"] == 1
        assert row["procedures"]["mine_ore"]["avg_ms"] == pytest.approx(2733.3, rel=0.01)
        assert row["procedures"]["smelt_ore"]["ok"] == 1
        assert row["procedures"]["smelt_ore"]["fail"] == 0

    @pytest.mark.asyncio
    async def test_appends_hourly_row_to_file(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        await agg.build_hourly(window_start=0.0, window_end=3600.0)
        await agg.build_hourly(window_start=3600.0, window_end=7200.0)
        lines = hourly_file.read_text().splitlines()
        assert len(lines) == 2
        rows = [json.loads(l) for l in lines]
        # Second row window differs from first
        assert rows[0]["hour"] != rows[1]["hour"]

    @pytest.mark.asyncio
    async def test_window_filters_events_outside_range(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        _write_events(events_file, [
            {"ts": 50.0,   "type": "gold_delta", "amount": 100},   # before window
            {"ts": 1800.0, "type": "gold_delta", "amount": 30},    # in window
            {"ts": 4000.0, "type": "gold_delta", "amount": 200},   # after window
        ])

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=1000.0, window_end=3600.0)
        assert row["gold"]["earned"] == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py::TestHourlyRollup -v`
Expected: FAIL — aggregator not defined.

- [ ] **Step 3: Implement `MetricsAggregator.build_hourly`**

Create `anima/monitor/metrics_pipeline/aggregator.py`:

```python
"""MetricsAggregator — hourly and daily rollups + retention trim."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger()


class MetricsAggregator:
    def __init__(
        self,
        *,
        events_file: Path,
        hourly_file: Path,
        daily_file: Path,
        db_path: Path,
        bus=None,
    ) -> None:
        self.events_file = events_file
        self.hourly_file = hourly_file
        self.daily_file = daily_file
        self.db_path = db_path
        self.bus = bus

    # ------------------------------------------------------------------
    # Hourly rollup
    # ------------------------------------------------------------------

    async def build_hourly(
        self, *, window_start: float, window_end: float,
    ) -> dict:
        """Build and persist one hourly row for [window_start, window_end)."""
        events = _read_events_in_window(
            self.events_file, window_start, window_end,
        )
        procs = await _aggregate_procedures(self.db_path, window_start, window_end)

        earned = sum(
            int(e["amount"]) for e in events
            if e["type"] == "gold_delta" and e.get("amount", 0) > 0
        )
        spent = sum(
            -int(e["amount"]) for e in events
            if e["type"] == "gold_delta" and e.get("amount", 0) < 0
        )

        hour_iso = (
            datetime.fromtimestamp(window_start, tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )

        row: dict[str, Any] = {
            "hour": hour_iso,
            "uptime_s": int(window_end - window_start),
            "procedures": procs,
            "cycles_completed": sum(
                1 for e in events if e["type"] == "cycle_complete"
            ),
            "phase_transitions": _count_phase_transitions(events),
            "gold": {
                "earned": earned,
                "spent": spent,
                "delta": earned - spent,
            },
            "deaths": sum(1 for e in events if e["type"] == "death"),
            "stuck_events": sum(
                1 for e in events if e["type"] == "stuck_event"
            ),
            "skills": _aggregate_skill_deltas(events),
        }

        _append_jsonl(self.hourly_file, row)
        if self.bus is not None:
            # Supervisor is a separate process; it cannot subscribe to this
            # in-process bus. state_publisher captures any bus event with a
            # `message` field into state.json's activity feed, which the
            # supervisor DOES read. Include a one-line summary alongside
            # the full row so both worlds work.
            hour_short = (row["hour"] or "")[:16].replace("T", " ")
            proc_stats = row.get("procedures", {})
            ok = sum(p.get("ok", 0) for p in proc_stats.values())
            fail = sum(p.get("fail", 0) for p in proc_stats.values())
            rate = (ok / (ok + fail)) if (ok + fail) else 1.0
            summary = (
                f"HOUR {hour_short}: cycles={row.get('cycles_completed', 0)} "
                f"gold+={row['gold'].get('delta', 0)} "
                f"deaths={row.get('deaths', 0)} "
                f"stuck={row.get('stuck_events', 0)} "
                f"proc_ok={rate:.0%}"
            )
            try:
                self.bus.publish("metrics.hourly_complete", {
                    "message": summary,
                    "importance": 2,
                    "row": row,
                })
            except Exception as e:
                logger.warning("metrics_hourly_publish_failed", error=str(e))
        return row


def _read_events_in_window(
    events_file: Path, start: float, end: float,
) -> list[dict]:
    if not events_file.exists():
        return []
    out = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = obj.get("ts", 0)
        if start <= ts < end:
            out.append(obj)
    return out


async def _aggregate_procedures(
    db_path: Path, start: float, end: float,
) -> dict[str, dict]:
    procs: dict[str, dict] = defaultdict(
        lambda: {"ok": 0, "fail": 0, "durations": []}
    )
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT procedure, result, duration_ms FROM action_logs "
            "WHERE timestamp >= ? AND timestamp < ?",
            (start, end),
        )
        async for proc, result, dur in cursor:
            slot = procs[proc]
            if result == "success":
                slot["ok"] += 1
            else:
                slot["fail"] += 1
            if dur is not None:
                slot["durations"].append(float(dur))

    # Replace durations list with avg_ms
    out: dict[str, dict] = {}
    for proc, slot in procs.items():
        durs = slot["durations"]
        avg = (sum(durs) / len(durs)) if durs else 0.0
        out[proc] = {"ok": slot["ok"], "fail": slot["fail"], "avg_ms": round(avg, 1)}
    return out


def _count_phase_transitions(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        if e["type"] != "phase_transition":
            continue
        key = f"{e.get('from_', '?')}->{e.get('to', '?')}"
        counts[key] += 1
    return dict(counts)


def _aggregate_skill_deltas(events: list[dict]) -> dict[str, dict]:
    per_skill: dict[int, dict] = {}
    for e in events:
        if e["type"] != "skill_delta":
            continue
        sid = e.get("skill_id")
        if sid is None:
            continue
        slot = per_skill.setdefault(sid, {"from": e.get("from"), "to": e.get("to")})
        # Keep the earliest "from" and the latest "to"
        if e.get("ts", 0) < slot.get("_earliest_ts", float("inf")):
            slot["from"] = e.get("from")
        slot["to"] = e.get("to")
        slot["_earliest_ts"] = e.get("ts", 0)
    # Strip internal bookkeeping
    for slot in per_skill.values():
        slot.pop("_earliest_ts", None)
    return {str(sid): slot for sid, slot in per_skill.items()}


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py::TestHourlyRollup -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/aggregator.py tests/monitor/test_metrics_aggregator.py
git commit -m "MetricsAggregator.build_hourly: window aggregation + file append

Reads events in [window_start, window_end) from metrics_events.jsonl,
queries action_logs for procedure stats in the same window, builds one
hourly row, appends to metrics_hourly.jsonl, publishes
metrics.hourly_complete on the bus."
```

---

## Task 5: `MetricsAggregator` — daily rollup + retention trim

**Files:**
- Modify: `anima/monitor/metrics_pipeline/aggregator.py`
- Modify: `tests/monitor/test_metrics_aggregator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/monitor/test_metrics_aggregator.py`:

```python
class TestDailyRollup:
    @pytest.mark.asyncio
    async def test_sums_hourly_rows_for_a_date(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        # Write two pre-existing hourly rows for 2026-04-17
        hourly_rows = [
            {
                "hour": "2026-04-17T00:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {"mine_ore": {"ok": 10, "fail": 2, "avg_ms": 3500}},
                "cycles_completed": 1,
                "phase_transitions": {},
                "gold": {"earned": 50, "spent": 20, "delta": 30},
                "deaths": 0,
                "stuck_events": 1,
                "skills": {"7": {"from": 63.0, "to": 63.2}},
            },
            {
                "hour": "2026-04-17T01:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {
                    "mine_ore": {"ok": 8, "fail": 0, "avg_ms": 3400},
                    "smelt_ore": {"ok": 3, "fail": 1, "avg_ms": 2000},
                },
                "cycles_completed": 2,
                "phase_transitions": {},
                "gold": {"earned": 80, "spent": 10, "delta": 70},
                "deaths": 1,
                "stuck_events": 0,
                "skills": {"7": {"from": 63.2, "to": 63.5}},
            },
        ]
        hourly_file.write_text("\n".join(json.dumps(r) for r in hourly_rows) + "\n")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-17")

        assert row["date"] == "2026-04-17"
        assert row["cycles_total"] == 3
        assert row["gold_earned"] == 130
        assert row["gold_spent"] == 30
        assert row["net_gold"] == 100
        assert row["deaths"] == 1
        assert row["stuck_events"] == 1
        # 21 ok / 3 fail → success_rate 0.875
        assert row["procedure_success_rate"] == pytest.approx(0.875, rel=0.01)
        # Top failures: mine_ore 2, smelt_ore 1
        assert row["top_failures"][0] == ["mine_ore", 2]
        assert row["skills_gained"]["7"] == pytest.approx(0.5, rel=0.01)

    @pytest.mark.asyncio
    async def test_missing_hourly_flags_hourly_missing(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        hourly_file.write_text("")  # empty
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-17")
        assert row["hourly_missing"] is True
        assert row["cycles_total"] == 0


class TestRetention:
    @pytest.mark.asyncio
    async def test_trim_removes_events_older_than_cutoff(
        self, events_file, tmp_path,
    ):
        _write_events(events_file, [
            {"ts": 100.0, "type": "gold_delta", "amount": 1},
            {"ts": 200.0, "type": "gold_delta", "amount": 2},
            {"ts": 300.0, "type": "gold_delta", "amount": 3},
        ])
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=tmp_path / "h.jsonl",
            daily_file=tmp_path / "d.jsonl",
            db_path=tmp_path / "t.db",
        )
        removed = await agg.trim_events(cutoff_ts=250.0)
        assert removed == 2
        remaining = [
            json.loads(l) for l in events_file.read_text().splitlines() if l.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["ts"] == 300.0

    @pytest.mark.asyncio
    async def test_trim_noop_when_nothing_to_remove(self, events_file, tmp_path):
        _write_events(events_file, [{"ts": 500.0, "type": "x"}])
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=tmp_path / "h.jsonl",
            daily_file=tmp_path / "d.jsonl",
            db_path=tmp_path / "t.db",
        )
        removed = await agg.trim_events(cutoff_ts=100.0)
        assert removed == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py::TestDailyRollup tests/monitor/test_metrics_aggregator.py::TestRetention -v`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Add daily rollup + trim methods**

Append to `anima/monitor/metrics_pipeline/aggregator.py` inside the `MetricsAggregator` class:

```python
    # ------------------------------------------------------------------
    # Daily rollup
    # ------------------------------------------------------------------

    async def build_daily(self, *, date_iso: str) -> dict:
        """Aggregate all hourly rows whose hour starts with date_iso."""
        hourly_rows = _read_hourly_rows_for_date(self.hourly_file, date_iso)

        cycles_total = sum(r.get("cycles_completed", 0) for r in hourly_rows)
        gold_earned = sum(r.get("gold", {}).get("earned", 0) for r in hourly_rows)
        gold_spent = sum(r.get("gold", {}).get("spent", 0) for r in hourly_rows)
        deaths = sum(r.get("deaths", 0) for r in hourly_rows)
        stuck_events = sum(r.get("stuck_events", 0) for r in hourly_rows)
        uptime_s = sum(r.get("uptime_s", 0) for r in hourly_rows)

        total_ok = 0
        total_fail = 0
        fail_by_proc: dict[str, int] = defaultdict(int)
        for r in hourly_rows:
            for proc, stats in r.get("procedures", {}).items():
                total_ok += stats.get("ok", 0)
                total_fail += stats.get("fail", 0)
                fail_by_proc[proc] += stats.get("fail", 0)
        total_runs = total_ok + total_fail
        success_rate = (total_ok / total_runs) if total_runs else 0.0

        top_failures = sorted(
            fail_by_proc.items(), key=lambda kv: (-kv[1], kv[0]),
        )[:5]

        # Skills gained = last hour's "to" minus first hour's "from"
        skills_gained: dict[str, float] = {}
        for r in hourly_rows:
            for sid, s in r.get("skills", {}).items():
                if sid not in skills_gained:
                    skills_gained[sid] = 0.0
                # Accumulate per-hour deltas
                skills_gained[sid] += float(
                    (s.get("to") or 0) - (s.get("from") or 0)
                )

        row: dict[str, Any] = {
            "date": date_iso,
            "uptime_s": uptime_s,
            "cycles_total": cycles_total,
            "gold_earned": gold_earned,
            "gold_spent": gold_spent,
            "net_gold": gold_earned - gold_spent,
            "deaths": deaths,
            "stuck_events": stuck_events,
            "procedure_success_rate": round(success_rate, 4),
            "top_failures": [[p, c] for p, c in top_failures],
            "skills_gained": {k: round(v, 2) for k, v in skills_gained.items()},
            "auto_recover_count": stuck_events,  # today these are the same
            "hourly_missing": len(hourly_rows) == 0,
        }

        _append_jsonl(self.daily_file, row)
        if self.bus is not None:
            try:
                self.bus.publish("metrics.daily_complete", row)
            except Exception as e:
                logger.warning("metrics_daily_publish_failed", error=str(e))
        return row

    # ------------------------------------------------------------------
    # Retention trim
    # ------------------------------------------------------------------

    async def trim_events(self, *, cutoff_ts: float) -> int:
        """Rewrite events file, dropping rows with ts < cutoff_ts.

        Returns the number of rows removed.
        """
        if not self.events_file.exists():
            return 0
        kept: list[str] = []
        removed = 0
        for line in self.events_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if obj.get("ts", 0) < cutoff_ts:
                removed += 1
            else:
                kept.append(line)

        if removed == 0:
            return 0

        tmp = self.events_file.with_suffix(".jsonl.tmp")
        tmp.write_text(("\n".join(kept) + "\n") if kept else "")
        tmp.replace(self.events_file)
        logger.info("metrics_trim_complete", removed=removed, kept=len(kept))
        return removed
```

Also add this helper at module level (outside any class):

```python
def _read_hourly_rows_for_date(path: Path, date_iso: str) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        hour = obj.get("hour", "")
        if hour.startswith(date_iso):
            out.append(obj)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/aggregator.py tests/monitor/test_metrics_aggregator.py
git commit -m "MetricsAggregator: daily rollup + retention trim

build_daily() sums yesterday's hourly rows into one daily entry
including success rate and top 5 failing procedures.
trim_events() rewrites metrics_events.jsonl atomically, dropping
rows older than cutoff_ts. No-op if nothing to remove."
```

---

## Task 6: `MetricsAggregator` — tick loop + backfill

**Files:**
- Modify: `anima/monitor/metrics_pipeline/aggregator.py`
- Modify: `tests/monitor/test_metrics_aggregator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/monitor/test_metrics_aggregator.py`:

```python
class TestTickLoop:
    @pytest.mark.asyncio
    async def test_run_once_builds_hourly_for_previous_hour(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        import time as _t

        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        # Pretend the current time is exactly at some hour boundary + 5s
        now = 1_700_000_000.0  # well into the future
        # Align to hour
        aligned = now - (now % 3600)
        await agg.run_once(now=aligned + 5.0)
        # Should have produced one hourly row for [aligned-3600, aligned]
        lines = [l for l in hourly_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        row = json.loads(lines[0])
        # Hour iso should be one hour before `aligned`
        import datetime as _dt
        expected_hour = _dt.datetime.fromtimestamp(
            aligned - 3600, tz=_dt.timezone.utc
        ).isoformat()
        assert row["hour"].startswith(expected_hour[:13])

    @pytest.mark.asyncio
    async def test_run_once_noop_within_same_hour(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        now = 1_700_000_005.0
        await agg.run_once(now=now)
        await agg.run_once(now=now + 30)
        # Should only produce one hourly row even with two ticks in the same hour
        lines = [l for l in hourly_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_backfill_missing_hours(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")
        # Seed one old hourly row, three hours ago
        base = 1_700_000_000.0 - (1_700_000_000.0 % 3600)
        three_hours_ago_iso = "2023-11-14T19:00:00+00:00"  # arbitrary past hour
        hourly_file.write_text(
            json.dumps({
                "hour": three_hours_ago_iso,
                "uptime_s": 3600, "procedures": {},
                "cycles_completed": 0, "phase_transitions": {},
                "gold": {"earned": 0, "spent": 0, "delta": 0},
                "deaths": 0, "stuck_events": 0, "skills": {},
            }) + "\n"
        )

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        now = base + 5.0
        # Should backfill at most 3 hours plus the current boundary hour
        n = await agg.run_once(now=now)
        assert n >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py::TestTickLoop -v`
Expected: FAIL — `run_once` not defined.

- [ ] **Step 3: Implement run_once**

Append to `MetricsAggregator` class in `anima/monitor/metrics_pipeline/aggregator.py`:

```python
    # ------------------------------------------------------------------
    # Scheduling / tick loop
    # ------------------------------------------------------------------

    _MAX_BACKFILL_HOURS = 6

    async def run_once(self, *, now: float | None = None) -> int:
        """Fire any rollups whose boundaries have passed since the last run.

        Returns the number of hourly rows written in this call (including
        backfills).
        """
        if now is None:
            now = time.time()

        last_iso = _last_hourly_iso(self.hourly_file)
        last_end = (
            _parse_hour_iso(last_iso) + 3600 if last_iso else now - 3600
        )
        # Align `now` down to the current hour boundary
        current_hour_start = now - (now % 3600)

        written = 0
        window_end = last_end
        attempted = 0
        while window_end < current_hour_start and attempted < self._MAX_BACKFILL_HOURS:
            window_start = window_end - 3600
            if window_end > window_start:
                await self.build_hourly(
                    window_start=window_start, window_end=window_end,
                )
                written += 1
            window_end += 3600
            attempted += 1

        # Daily rollup once per UTC midnight
        today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        yesterday = (
            datetime.fromtimestamp(now - 86400, tz=timezone.utc).date().isoformat()
        )
        if _should_build_daily(self.daily_file, yesterday):
            await self.build_daily(date_iso=yesterday)
            cutoff = now - 30 * 86400  # 30-day retention
            await self.trim_events(cutoff_ts=cutoff)

        return written

    async def run_forever(self) -> None:
        """Background task — call run_once() every 60s."""
        import asyncio
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("metrics_run_once_failed", error=str(e))
            await asyncio.sleep(60)
```

And these helpers at module level:

```python
def _last_hourly_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line).get("hour")
        except Exception:
            continue
    return last


def _parse_hour_iso(iso: str) -> float:
    # Handles `YYYY-MM-DDTHH:MM:SS+00:00`
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _should_build_daily(daily_file: Path, date_iso: str) -> bool:
    if not daily_file.exists():
        return True
    for line in daily_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("date") == date_iso:
                return False  # already built
        except Exception:
            continue
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_aggregator.py -v`
Expected: all tests PASS (including TestTickLoop).

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/aggregator.py tests/monitor/test_metrics_aggregator.py
git commit -m "MetricsAggregator.run_once: hourly/daily ticking + backfill

Called from a 60s loop. Determines the last hourly row written,
fires build_hourly() for each missing hour up to MAX_BACKFILL_HOURS=6,
runs build_daily() for yesterday if not yet done, and triggers the
30-day retention trim at the daily boundary."
```

---

## Task 7: `MetricsAlertDetector`

**Files:**
- Create: `anima/monitor/metrics_pipeline/alerts.py`
- Test: `tests/monitor/test_metrics_alerts.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/monitor/test_metrics_alerts.py`:

```python
"""Tests for MetricsAlertDetector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.monitor.metrics_pipeline.alerts import MetricsAlertDetector


@pytest.fixture
def alerts_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_alerts.jsonl"


def _hourly_row(cycles=2, deaths=0, stuck_events=0, procs_ok=10, procs_fail=1,
                hour="2026-04-17T12:00:00+00:00"):
    return {
        "hour": hour,
        "uptime_s": 3600,
        "procedures": {
            "mine_ore": {"ok": procs_ok, "fail": procs_fail, "avg_ms": 3500},
        },
        "cycles_completed": cycles,
        "phase_transitions": {},
        "gold": {"earned": 50, "spent": 10, "delta": 40},
        "deaths": deaths,
        "stuck_events": stuck_events,
        "skills": {},
    }


class TestAlertRules:
    def test_single_death_fires_alert(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(deaths=1))
        assert any(a["rule"] == "death" for a in alerts)

    def test_stuck_events_over_5_fires(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(stuck_events=6))
        assert any(a["rule"] == "stuck_events" for a in alerts)

    def test_stuck_events_at_5_does_not_fire(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(stuck_events=5))
        assert all(a["rule"] != "stuck_events" for a in alerts)

    def test_cycles_regression_requires_3_consecutive(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        # Establish a baseline of 4 cycles/hour over 6 hours
        for _ in range(6):
            det.check(_hourly_row(cycles=4))
        # Two low hours — should NOT fire yet
        a1 = det.check(_hourly_row(cycles=1))
        a2 = det.check(_hourly_row(cycles=1))
        assert all(a["rule"] != "cycles_regression" for a in (a1 + a2))
        # Third low hour — should fire
        a3 = det.check(_hourly_row(cycles=1))
        assert any(a["rule"] == "cycles_regression" for a in a3)

    def test_cycles_recovery_clears_streak(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        for _ in range(6):
            det.check(_hourly_row(cycles=4))
        det.check(_hourly_row(cycles=1))
        det.check(_hourly_row(cycles=1))
        # Recovery hour
        det.check(_hourly_row(cycles=4))
        # Next low hour is only streak-of-1 again
        alerts = det.check(_hourly_row(cycles=1))
        assert all(a["rule"] != "cycles_regression" for a in alerts)

    def test_procedure_success_below_60pct_for_2h(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        # First low-success hour
        a1 = det.check(_hourly_row(procs_ok=3, procs_fail=7))  # 30%
        assert all(a["rule"] != "low_success_rate" for a in a1)
        # Second consecutive low hour
        a2 = det.check(_hourly_row(procs_ok=5, procs_fail=5))  # 50%
        assert any(a["rule"] == "low_success_rate" for a in a2)

    def test_alerts_persisted_to_file(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        det.check(_hourly_row(deaths=1))
        lines = alerts_file.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["rule"] == "death"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/monitor/test_metrics_alerts.py -v`
Expected: FAIL — module not defined.

- [ ] **Step 3: Implement alerts**

Create `anima/monitor/metrics_pipeline/alerts.py`:

```python
"""MetricsAlertDetector — threshold rules over hourly rollup rows."""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

import structlog

logger = structlog.get_logger()


class MetricsAlertDetector:
    """Evaluates each new hourly row against a small set of rules.

    Rules:
      - death: any hour with deaths > 0
      - stuck_events: any hour with stuck_events > 5
      - cycles_regression: 3 consecutive hours with cycles <= 0.5 * prior-6h-mean
      - low_success_rate: 2 consecutive hours with proc success rate < 60%
    """

    STUCK_THRESHOLD = 5
    REGRESSION_MULT = 0.5
    REGRESSION_BASELINE_HOURS = 6
    REGRESSION_CONSECUTIVE_HOURS = 3
    SUCCESS_RATE_THRESHOLD = 0.60
    SUCCESS_RATE_CONSECUTIVE_HOURS = 2

    def __init__(self, *, alerts_file: Path, bus=None) -> None:
        self.alerts_file = alerts_file
        self.bus = bus
        self._cycles_history: deque[int] = deque(
            maxlen=self.REGRESSION_BASELINE_HOURS + self.REGRESSION_CONSECUTIVE_HOURS
        )
        self._success_history: deque[float] = deque(maxlen=4)

    def check(self, hourly_row: dict) -> list[dict]:
        fired: list[dict] = []

        # Rule: death
        if hourly_row.get("deaths", 0) > 0:
            fired.append({
                "rule": "death",
                "value": hourly_row["deaths"],
                "message": f"☠ deaths={hourly_row['deaths']} in hour {hourly_row.get('hour', '?')}",
            })

        # Rule: stuck_events
        stuck = hourly_row.get("stuck_events", 0)
        if stuck > self.STUCK_THRESHOLD:
            fired.append({
                "rule": "stuck_events",
                "value": stuck,
                "message": f"⚠ stuck_events={stuck} exceeds {self.STUCK_THRESHOLD}/h",
            })

        # Rule: cycles_regression
        self._cycles_history.append(hourly_row.get("cycles_completed", 0))
        reg = self._check_cycles_regression()
        if reg is not None:
            fired.append(reg)

        # Rule: low_success_rate
        rate = _success_rate(hourly_row)
        self._success_history.append(rate)
        sr = self._check_success_rate()
        if sr is not None:
            fired.append(sr)

        # Persist + optionally publish
        for alert in fired:
            alert_ts = time.time()
            line = json.dumps({"ts": alert_ts, **alert}, ensure_ascii=False)
            try:
                self.alerts_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.alerts_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.warning("metrics_alert_persist_failed", error=str(e))
            if self.bus is not None:
                try:
                    self.bus.publish("metrics.alert", alert)
                except Exception as e:
                    logger.warning("metrics_alert_publish_failed", error=str(e))

        return fired

    def _check_cycles_regression(self) -> dict | None:
        hist = list(self._cycles_history)
        tail = hist[-self.REGRESSION_CONSECUTIVE_HOURS:]
        if len(tail) < self.REGRESSION_CONSECUTIVE_HOURS:
            return None
        baseline_slice = hist[
            -(self.REGRESSION_CONSECUTIVE_HOURS + self.REGRESSION_BASELINE_HOURS)
            : -self.REGRESSION_CONSECUTIVE_HOURS
        ]
        if not baseline_slice:
            return None
        baseline = sum(baseline_slice) / len(baseline_slice)
        threshold = self.REGRESSION_MULT * baseline
        if baseline > 0 and all(v <= threshold for v in tail):
            return {
                "rule": "cycles_regression",
                "baseline": round(baseline, 2),
                "recent": tail,
                "message": (
                    f"⚠ cycles dropped to {tail} (baseline {baseline:.1f}/h)"
                ),
            }
        return None

    def _check_success_rate(self) -> dict | None:
        tail = list(self._success_history)[-self.SUCCESS_RATE_CONSECUTIVE_HOURS:]
        if len(tail) < self.SUCCESS_RATE_CONSECUTIVE_HOURS:
            return None
        if all(r < self.SUCCESS_RATE_THRESHOLD for r in tail):
            return {
                "rule": "low_success_rate",
                "recent": [round(r, 3) for r in tail],
                "message": (
                    f"⚠ procedure success rate below "
                    f"{self.SUCCESS_RATE_THRESHOLD:.0%} for "
                    f"{self.SUCCESS_RATE_CONSECUTIVE_HOURS}h"
                ),
            }
        return None


def _success_rate(hourly_row: dict) -> float:
    total_ok = 0
    total_fail = 0
    for stats in hourly_row.get("procedures", {}).values():
        total_ok += stats.get("ok", 0)
        total_fail += stats.get("fail", 0)
    total = total_ok + total_fail
    return (total_ok / total) if total else 1.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/monitor/test_metrics_alerts.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add anima/monitor/metrics_pipeline/alerts.py tests/monitor/test_metrics_alerts.py
git commit -m "MetricsAlertDetector: 4 threshold rules on hourly rollup

Rules:
  - death: any hour with deaths>0 fires immediately
  - stuck_events: >5 in one hour
  - cycles_regression: 3 consecutive hours at <=0.5x trailing-6h mean
  - low_success_rate: proc success <60% for 2 consecutive hours

Writes to data/metrics_alerts.jsonl; publishes metrics.alert to bus
if one is attached."
```

---

## Task 8: Wire into `avatar.py` startup + supervisor bus emit

**Files:**
- Modify: `anima/core/avatar.py`
- Modify: `tools/supervisor.py`

- [ ] **Step 1: Inspect avatar.py for the right startup location**

Read `anima/core/avatar.py` around the section where `bus`, `memory_db`, and other monitor services are started. Note the file path for the bus object (typically `self.bus`) and the data directory root.

- [ ] **Step 2: Add startup wiring in `anima/core/avatar.py`**

In `avatar.py`, near the bottom of the async `start()` / `run()` method (after bus is created and memory DB is open), add:

```python
        # --- Metrics pipeline ---
        from anima.monitor.metrics_pipeline.collector import MetricsCollector
        from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator
        from anima.monitor.metrics_pipeline.alerts import MetricsAlertDetector
        from anima.memory.database import DB_FILE as _METRICS_DB

        data_dir = Path(__file__).parent.parent.parent / "data"
        metrics_events_file = data_dir / "metrics_events.jsonl"

        metrics_collector = MetricsCollector(events_file=metrics_events_file)
        metrics_collector.attach_bus(self.bus)
        self._metrics_collector = metrics_collector

        metrics_aggregator = MetricsAggregator(
            events_file=metrics_events_file,
            hourly_file=data_dir / "metrics_hourly.jsonl",
            daily_file=data_dir / "metrics_daily.jsonl",
            db_path=_METRICS_DB,
            bus=self.bus,
        )
        self._metrics_aggregator = metrics_aggregator

        metrics_alerts = MetricsAlertDetector(
            alerts_file=data_dir / "metrics_alerts.jsonl",
            bus=self.bus,
        )
        self._metrics_alerts = metrics_alerts
        self.bus.subscribe(
            "metrics.hourly_complete",
            lambda _t, row: metrics_alerts.check(row),
        )

        # Periodic state diff (every 5 s) + aggregator run_forever()
        async def _state_diff_loop() -> None:
            import asyncio, json
            from pathlib import Path as _P
            state_file = data_dir / "state.json"
            prev: dict | None = None
            while True:
                try:
                    if state_file.exists():
                        curr = json.loads(state_file.read_text())
                        metrics_collector._diff_state(prev, curr)
                        prev = curr
                except Exception as e:
                    logger.warning("metrics_state_poll_failed", error=str(e))
                await asyncio.sleep(5)

        async def _action_log_poll_loop() -> None:
            import asyncio
            while True:
                try:
                    await metrics_collector.poll_action_logs()
                except Exception as e:
                    logger.warning("metrics_action_log_poll_failed", error=str(e))
                await asyncio.sleep(30)

        asyncio.create_task(_state_diff_loop(), name="metrics_state_loop")
        asyncio.create_task(_action_log_poll_loop(), name="metrics_action_log_loop")
        asyncio.create_task(
            metrics_aggregator.run_forever(), name="metrics_aggregator_loop",
        )
        logger.info("metrics_pipeline_started")
```

Also add this import at the top of `avatar.py` (if `Path` isn't already imported):

```python
from pathlib import Path
```

- [ ] **Step 3: Add bus emit in supervisor's auto_recover()**

In `tools/supervisor.py`, locate `auto_recover()`:

```python
def auto_recover(proc, reason, extra_args):
    _alert(f"AUTO-RECOVER: {reason}")
```

Supervisor does not have direct bus access (it's a separate process). Instead, have the supervisor append a line to `metrics_events.jsonl` directly as a fallback, or add a new event type that a running agent can read from a file. Simplest: record directly.

Modify `auto_recover()` to also call:

```python
    # Record supervisor-side auto-recover as a metric event so the
    # agent's pipeline sees stuck_events regardless of whether the
    # new agent instance has started yet.
    try:
        import json, time
        events_file = ROOT / "data" / "metrics_events.jsonl"
        events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "type": "stuck_event",
                "reason": reason,
                "source": "supervisor",
            }) + "\n")
    except Exception:
        pass
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all previously-passing tests still pass; no new failures introduced.

- [ ] **Step 5: Commit**

```bash
git add anima/core/avatar.py tools/supervisor.py
git commit -m "Wire metrics pipeline into agent startup + supervisor emit

avatar.py starts three background tasks on agent init:
  - state_diff_loop (5 s) — polls state.json, emits gold/skill/death events
  - action_log_poll_loop (30 s) — emits procedure_end events for new rows
  - aggregator.run_forever() — hourly/daily rollups + retention

supervisor.py's auto_recover() now also appends a stuck_event row to
data/metrics_events.jsonl directly so stuck counts accumulate even
across agent restarts."
```

---

## Task 9: Supervisor stdio — register metrics topics as notable

**Files:**
- Modify: `tools/supervisor.py`

The aggregator (Task 4) already publishes `metrics.hourly_complete` with a preformatted `message` field. state_publisher captures any bus event with `message` into state.json's activity feed, and supervisor's existing notable-topic branch already prints those as `[HH:MM:SS] EVENT: <message>`. We only need to add the two topic names to `_NOTABLE_TOPICS` — no custom formatting logic.

- [ ] **Step 1: Extend `_NOTABLE_TOPICS`**

In `tools/supervisor.py`, locate the existing `_NOTABLE_TOPICS = { ... }` block and add two entries:

```python
_NOTABLE_TOPICS = {
    "planner.stopped",
    "planner.death",
    "planner.critical_hp",
    "metrics.alert",           # NEW
    "metrics.hourly_complete", # NEW (aggregator emits preformatted message)
}
```

- [ ] **Step 2: Update MetricsAlertDetector bus publish to include a message field**

In `anima/monitor/metrics_pipeline/alerts.py`, the bus publish block already passes the alert dict which contains a `message` key — no change needed. Verify by grepping:

```bash
grep -A3 'self.bus.publish("metrics.alert"' anima/monitor/metrics_pipeline/alerts.py
```

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: no regressions. Supervisor stdio formatting is not unit-tested; acceptance is manual on the live agent.

- [ ] **Step 4: Commit**

```bash
git add tools/supervisor.py
git commit -m "Supervisor stdio: register metrics topics as notable events

metrics.hourly_complete and metrics.alert now surface as EVENT lines
alongside the existing planner/expedition events. The aggregator
publishes a preformatted message so no custom formatter is needed
on the supervisor side.

Example stdio output after the first full hour:
  [13:00:02] EVENT: HOUR 2026-04-17 12:00: cycles=2 gold+=67 deaths=0 stuck=1 proc_ok=93%
  [13:00:02] EVENT: ☠ deaths=1 in hour 2026-04-17T12:00:00+00:00"
```

- [ ] **Step 2: Manual verification (no automated test for stdio formatting)**

Run the supervisor for a minute and confirm that when you manually append an `expedition.cycle_complete` event followed by a synthetic hourly rollup to state.json's activity feed, you see the formatted HOUR line. (This is an opportunistic check — if the wiring is wrong, the next live hour will expose it.)

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: no regressions. Supervisor is not unit-tested for stdio directly — acceptance is manual.

- [ ] **Step 4: Commit**

```bash
git add tools/supervisor.py
git commit -m "Supervisor stdio: format hourly rollup and surface alerts

metrics.hourly_complete becomes:
  [HH:MM:SS] HOUR 2026-04-17 12:00: cycles=2 gold+=67 deaths=0 stuck=1 proc_ok=93%

metrics.alert becomes:
  ⚠ [HH:MM:SS] METRIC ALERT: <message>

Both route through the same _print_status() event scan that surfaces
notable procedure outcomes."
```

---

## Task 10: CLI tool (`tools/metrics.py`)

**Files:**
- Create: `tools/metrics.py`
- Test: `tests/tools/test_metrics_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_metrics_cli.py`:

```python
"""Tests for tools/metrics.py CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
CLI = ROOT / "tools" / "metrics.py"


def _run(args: list[str], env_overrides: dict) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True, text=True, env=env,
    )


def test_today_with_no_data_prints_placeholder(tmp_path: Path):
    env = {
        "ANIMA_METRICS_HOURLY": str(tmp_path / "h.jsonl"),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--today"], env)
    assert result.returncode == 0
    assert "no data" in result.stdout.lower() or "cycles" in result.stdout.lower()


def test_today_json_mode(tmp_path: Path):
    hourly = tmp_path / "h.jsonl"
    import time as _t
    today_prefix = _t.strftime("%Y-%m-%d", _t.gmtime())
    hourly.write_text(json.dumps({
        "hour": f"{today_prefix}T00:00:00+00:00",
        "uptime_s": 3600,
        "procedures": {"mine_ore": {"ok": 10, "fail": 2, "avg_ms": 3500}},
        "cycles_completed": 2,
        "phase_transitions": {},
        "gold": {"earned": 50, "spent": 10, "delta": 40},
        "deaths": 0, "stuck_events": 1, "skills": {},
    }) + "\n")

    env = {
        "ANIMA_METRICS_HOURLY": str(hourly),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--today", "--json"], env)
    assert result.returncode == 0
    data = json.loads(result.stdout.strip() or "[]")
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["cycles_completed"] == 2


def test_top_failures(tmp_path: Path):
    hourly = tmp_path / "h.jsonl"
    import time as _t
    today_prefix = _t.strftime("%Y-%m-%d", _t.gmtime())
    hourly.write_text(json.dumps({
        "hour": f"{today_prefix}T00:00:00+00:00",
        "uptime_s": 3600,
        "procedures": {
            "mine_ore": {"ok": 3, "fail": 10, "avg_ms": 3500},
            "smelt_ore": {"ok": 5, "fail": 2, "avg_ms": 2000},
        },
        "cycles_completed": 0, "phase_transitions": {},
        "gold": {"earned": 0, "spent": 0, "delta": 0},
        "deaths": 0, "stuck_events": 0, "skills": {},
    }) + "\n")
    env = {
        "ANIMA_METRICS_HOURLY": str(hourly),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--top-failures", "24h"], env)
    assert result.returncode == 0
    assert "mine_ore" in result.stdout
    # mine_ore failures should be listed before smelt_ore
    assert result.stdout.find("mine_ore") < result.stdout.find("smelt_ore")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tools/test_metrics_cli.py -v`
Expected: FAIL — CLI does not exist.

- [ ] **Step 3: Implement the CLI**

Create `tools/metrics.py`:

```python
#!/usr/bin/env python3
"""Metrics CLI — query hourly/daily rollups and raw events.

Examples:
  tools/metrics.py --today
  tools/metrics.py --last 7d
  tools/metrics.py --top-failures 24h
  tools/metrics.py --compare today yesterday
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _path(env_key: str, default_name: str) -> Path:
    return Path(os.environ.get(env_key) or (ROOT / "data" / default_name))


HOURLY_FILE = lambda: _path("ANIMA_METRICS_HOURLY", "metrics_hourly.jsonl")
DAILY_FILE = lambda: _path("ANIMA_METRICS_DAILY", "metrics_daily.jsonl")
EVENTS_FILE = lambda: _path("ANIMA_METRICS_EVENTS", "metrics_events.jsonl")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _rows_for_today(hourly: list[dict]) -> list[dict]:
    prefix = _today_iso()
    return [r for r in hourly if r.get("hour", "").startswith(prefix)]


def _parse_window(text: str) -> tuple[float, float]:
    """Parse a window string like '24h' or '7d' into (start_ts, end_ts)."""
    now = time.time()
    if text.endswith("h"):
        hours = int(text[:-1])
        return now - hours * 3600, now
    if text.endswith("d"):
        days = int(text[:-1])
        return now - days * 86400, now
    raise SystemExit(f"Invalid window: {text}")


def cmd_today(args) -> int:
    hourly = _read_jsonl(HOURLY_FILE())
    rows = _rows_for_today(hourly)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(f"No hourly data yet for {_today_iso()}")
        return 0
    print(f"Today ({_today_iso()}) — {len(rows)} hour(s):")
    print(f"{'hour':<17} {'cyc':>4} {'gold':>6} {'deaths':>6} {'stuck':>6} {'ok%':>5}")
    for r in rows:
        hour = r.get("hour", "")[:16].replace("T", " ")
        cyc = r.get("cycles_completed", 0)
        g = r.get("gold", {}).get("delta", 0)
        deaths = r.get("deaths", 0)
        stuck = r.get("stuck_events", 0)
        ok = sum(p.get("ok", 0) for p in r.get("procedures", {}).values())
        fail = sum(p.get("fail", 0) for p in r.get("procedures", {}).values())
        rate = (ok / (ok + fail) * 100) if (ok + fail) else 100.0
        print(f"{hour:<17} {cyc:>4} {g:+6d} {deaths:>6} {stuck:>6} {rate:>4.0f}%")
    return 0


def cmd_last(args) -> int:
    start, end = _parse_window(args.last)
    hourly = _read_jsonl(HOURLY_FILE())
    rows = [
        r for r in hourly
        if start <= _parse_hour(r.get("hour", "")) < end
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    print(f"Last {args.last} — {len(rows)} rows")
    for r in rows:
        hour = r.get("hour", "")[:16].replace("T", " ")
        print(f"  {hour}  cycles={r.get('cycles_completed', 0)}  "
              f"gold={r.get('gold', {}).get('delta', 0):+d}  "
              f"deaths={r.get('deaths', 0)}")
    return 0


def cmd_top_failures(args) -> int:
    start, end = _parse_window(args.top_failures)
    hourly = _read_jsonl(HOURLY_FILE())
    rows = [
        r for r in hourly
        if start <= _parse_hour(r.get("hour", "")) < end
    ]
    fail_count: dict[str, int] = defaultdict(int)
    for r in rows:
        for proc, stats in r.get("procedures", {}).items():
            fail_count[proc] += stats.get("fail", 0)
    ranked = sorted(fail_count.items(), key=lambda kv: (-kv[1], kv[0]))
    if args.json:
        print(json.dumps(ranked, ensure_ascii=False))
        return 0
    print(f"Top failures in last {args.top_failures}:")
    for proc, c in ranked[:10]:
        print(f"  {proc:<30} {c}")
    return 0


def cmd_compare(args) -> int:
    a_label, b_label = args.compare
    hourly = _read_jsonl(HOURLY_FILE())
    daily = _read_jsonl(DAILY_FILE())

    def _stats_for_day(label: str) -> dict:
        if label == "today":
            rows = _rows_for_today(hourly)
            return {
                "cycles": sum(r.get("cycles_completed", 0) for r in rows),
                "gold_delta": sum(r.get("gold", {}).get("delta", 0) for r in rows),
                "deaths": sum(r.get("deaths", 0) for r in rows),
                "stuck": sum(r.get("stuck_events", 0) for r in rows),
            }
        if label == "yesterday":
            y = (
                datetime.now(timezone.utc).date() - timedelta(days=1)
            ).isoformat()
            for d in daily:
                if d.get("date") == y:
                    return {
                        "cycles": d.get("cycles_total", 0),
                        "gold_delta": d.get("net_gold", 0),
                        "deaths": d.get("deaths", 0),
                        "stuck": d.get("stuck_events", 0),
                    }
            return {"cycles": 0, "gold_delta": 0, "deaths": 0, "stuck": 0}
        raise SystemExit(f"Unsupported compare label: {label}")

    a = _stats_for_day(a_label)
    b = _stats_for_day(b_label)
    if args.json:
        print(json.dumps({a_label: a, b_label: b}, ensure_ascii=False))
        return 0
    print(f"{'metric':<15} {a_label:>10} {b_label:>10} {'delta':>10}")
    for key in ("cycles", "gold_delta", "deaths", "stuck"):
        print(f"{key:<15} {a[key]:>10} {b[key]:>10} {a[key] - b[key]:>+10}")
    return 0


def _parse_hour(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def main() -> int:
    p = argparse.ArgumentParser(prog="metrics", description=__doc__)
    p.add_argument("--today", action="store_true", help="Today's hourly rollups")
    p.add_argument("--last", metavar="Nh|Nd", help="Last N hours/days of hourly rows")
    p.add_argument("--top-failures", metavar="WINDOW", help="Top failing procedures in window")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="Compare two windows")
    p.add_argument("--json", action="store_true", help="Raw JSON output")
    args = p.parse_args()

    if args.today:
        return cmd_today(args)
    if args.last:
        return cmd_last(args)
    if args.top_failures:
        return cmd_top_failures(args)
    if args.compare:
        return cmd_compare(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tools/test_metrics_cli.py -v`
Expected: all 3 tests PASS.

Optional sanity: `uv run python tools/metrics.py --help` should print usage.

- [ ] **Step 5: Commit**

```bash
git add tools/metrics.py tests/tools/test_metrics_cli.py
git commit -m "Add tools/metrics.py CLI for hourly/daily query

Commands:
  --today            hourly rollups for today
  --last 7d | 24h    rows within trailing window
  --top-failures W   procedure fail ranking in window
  --compare A B      delta between two periods
  --json             raw JSON

Reads data/metrics_*.jsonl directly — no agent required."
```

---

## Task 11: Integration smoke test + .gitignore

**Files:**
- Modify: `.gitignore`
- Test: `tests/monitor/test_metrics_integration.py`

- [ ] **Step 1: Update .gitignore**

Add to `.gitignore`:

```
data/metrics_events.jsonl
data/metrics_hourly.jsonl
data/metrics_daily.jsonl
data/metrics_alerts.jsonl
```

- [ ] **Step 2: Write the integration test**

Create `tests/monitor/test_metrics_integration.py`:

```python
"""End-to-end test of the metrics pipeline with a fresh tmp directory."""
from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite
import pytest

from anima.monitor.metrics_pipeline import record
from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator
from anima.monitor.metrics_pipeline.alerts import MetricsAlertDetector
from anima.monitor.metrics_pipeline.collector import MetricsCollector


@pytest.mark.asyncio
async def test_end_to_end(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    hourly = tmp_path / "hourly.jsonl"
    daily = tmp_path / "daily.jsonl"
    alerts = tmp_path / "alerts.jsonl"
    db = tmp_path / "anima.db"

    async with aiosqlite.connect(db) as conn:
        await conn.execute("""
            CREATE TABLE action_logs (
                timestamp REAL, agent TEXT, procedure TEXT,
                location_x INTEGER, location_y INTEGER,
                result TEXT, message TEXT, duration_ms REAL, details TEXT
            )
        """)
        await conn.executemany(
            "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (100.0, "G", "mine_ore", 0, 0, "success", "", 3500.0, "{}"),
                (200.0, "G", "mine_ore", 0, 0, "blocked", "", 2000.0, "{}"),
                (300.0, "G", "smelt_ore", 0, 0, "success", "", 2000.0, "{}"),
            ],
        )
        await conn.commit()

    # Emit a handful of events spanning one hour
    for type_, kw in [
        ("gold_delta", {"amount": 50}),
        ("gold_delta", {"amount": -10}),
        ("cycle_complete", {"cycles_completed": 1, "duration_s": 180.0}),
        ("death", {"pos": [0, 0], "hp_before": 15}),
        ("stuck_event", {"reason": "idle"}),
    ]:
        record(type_, events_file=events, **kw)

    # Set event timestamps to fall into a window [100, 3700]
    lines = events.read_text().splitlines()
    base = 100.0
    aligned = []
    for i, line in enumerate(lines):
        obj = json.loads(line)
        obj["ts"] = base + i * 10
        aligned.append(json.dumps(obj))
    events.write_text("\n".join(aligned) + "\n")

    agg = MetricsAggregator(
        events_file=events, hourly_file=hourly, daily_file=daily, db_path=db,
    )
    hourly_row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

    # Sanity
    assert hourly_row["cycles_completed"] == 1
    assert hourly_row["deaths"] == 1
    assert hourly_row["gold"]["earned"] == 50
    assert hourly_row["gold"]["spent"] == 10
    assert hourly_row["procedures"]["mine_ore"]["ok"] == 1
    assert hourly_row["procedures"]["mine_ore"]["fail"] == 1

    # Alerts — should trigger on the one death
    det = MetricsAlertDetector(alerts_file=alerts)
    fired = det.check(hourly_row)
    assert any(a["rule"] == "death" for a in fired)
    assert alerts.exists()
```

- [ ] **Step 3: Run all tests + lint**

Run: `.venv/bin/pytest tests/ -q`
Expected: all tests PASS, including the new integration test.

Run: `.venv/bin/ruff check anima/monitor/metrics_pipeline tools/metrics.py tests/monitor tests/tools`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add .gitignore tests/monitor/test_metrics_integration.py
git commit -m "Metrics pipeline integration smoke test + gitignore

End-to-end scenario: emit events, insert action_log rows, build
hourly rollup, run alert detector. Verifies all three modules wire
together with realistic inputs and that the death rule fires on the
emitted event."
```

---

## Post-implementation verification

Over the next 48 hours of agent uptime:

1. Run `.venv/bin/ruff check anima/monitor/metrics_pipeline/ tools/metrics.py` — must show no errors.
2. `tools/metrics.py --today` — must print a non-empty table after the first hour boundary.
3. Supervisor stdio — must show at least one `HOUR 2026-04-17 HH:00:` line per live hour.
4. `data/metrics_daily.jsonl` — must contain a row at midnight UTC.
5. Deliberate regression check: temporarily break mining (e.g., set a too-aggressive bank cooldown), confirm `⚠ METRIC ALERT: cycles_regression` fires within 3 hours.
