"""Tests for MetricsCollector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.monitor.metrics_pipeline.collector import MetricsCollector


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_events.jsonl"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


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

    def test_transient_hp_flicker_does_not_double_count_death(
        self, events_file: Path,
    ):
        """A stale-vitals flicker (0 -> 1 -> 0) during the ghost state must
        count as ONE death, not two. The brief 1-hp reading is not a real
        resurrection (which restores meaningful HP), so the death-edge stays
        debounced and the relapse to 0 does not fire a second death."""
        col = MetricsCollector(events_file=events_file)
        # Real death.
        col._diff_state(_make_state(hp=20), _make_state(hp=0))
        # Stale-packet flicker up to 1 hp (still a ghost), then back to 0.
        col._diff_state(_make_state(hp=0), _make_state(hp=1))
        col._diff_state(_make_state(hp=1), _make_state(hp=0))
        death_events = [e for e in _read(events_file) if e["type"] == "death"]
        assert len(death_events) == 1

    def test_real_resurrection_then_new_death_counts_twice(
        self, events_file: Path,
    ):
        """A genuine resurrection (HP restored to a meaningful share of max)
        re-arms the death edge, so a subsequent death IS counted again."""
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(hp=20, hp_max=112), _make_state(hp=0, hp_max=112))
        # Healer res: HP back up well above the flicker threshold.
        col._diff_state(_make_state(hp=0, hp_max=112), _make_state(hp=80, hp_max=112))
        # Second real death.
        col._diff_state(_make_state(hp=80, hp_max=112), _make_state(hp=0, hp_max=112))
        death_events = [e for e in _read(events_file) if e["type"] == "death"]
        assert len(death_events) == 2

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
